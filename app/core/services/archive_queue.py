"""统一存档任务队列。

规则：
- 每个任务绑定入队时捕获的工作区 Database（工作区热切换不影响执行中的任务）；
- 同一 (工作区, 源) 内严格 FIFO 串行，不同源并行；
- 同一条目在队列中/执行中时拒绝重复入队；
- 一切状态变化经 EventBus 广播（web 层经 SSE 推给前端）；
- 启动时把上次中断遗留的 pending 条目重新入队。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field

from app.core.services import fulltext

log = logging.getLogger("deepaggregator.archive_queue")

HISTORY_LIMIT = 100


class EventBus:
    """进程内事件广播；订阅者各持一个有界队列，慢消费者丢旧事件。"""

    def __init__(self, capacity: int = 256):
        self._subs: set[asyncio.Queue] = set()
        self._capacity = capacity

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._capacity)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event subscriber too slow, dropping event %s", event.get("type"))


@dataclass
class ArchiveTask:
    ws_path: str
    ws_name: str
    db: object
    item_id: int
    source_id: int
    title: str = ""
    state: str = "queued"  # queued | running | done | failed
    request_index: int = 0
    request_count: int = 0
    error: str = ""
    result: dict = field(default_factory=dict)

    def view(self) -> dict:
        return {
            "item_id": self.item_id,
            "source_id": self.source_id,
            "title": self.title,
            "state": self.state,
            "request_index": self.request_index,
            "request_count": self.request_count,
            "error": self.error,
            "kind": self.result.get("kind", ""),
            "name": self.result.get("name", ""),
            "size": self.result.get("size", 0),
            "workspace": self.ws_name,
        }


class ArchiveQueue:
    def __init__(self, core):
        self.core = core
        self.bus = EventBus()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runners: dict[tuple[str, int], asyncio.Task] | None = None
        self._scheduled = 0  # 已入线程安全调度、尚未在事件循环落地的入队数
        self._pending: dict[tuple[str, int], deque[ArchiveTask]] = {}
        self._active: dict[tuple[str, int], ArchiveTask] = {}
        self._history: deque[ArchiveTask] = deque(maxlen=HISTORY_LIMIT)

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        # 丢弃未执行任务，取消在跑任务：CancelledError 不会被执行器的 except 捕获，
        # 被取消任务对应条目保持 pending，由下次启动的 recover() 重新入队
        self._pending.clear()
        self._active.clear()
        self._scheduled = 0
        runners = list((self._runners or {}).values())
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        self._runners = None
        self._loop = None

    def recover(self, db) -> int:
        """把上次中断遗留的 pending 条目重新入队，返回数量。"""
        rows = db.all(
            "SELECT id, source_id FROM items WHERE fulltext_state = 'pending'"
        )
        for row in rows:
            self.enqueue(db, row["id"], row["source_id"])
        return len(rows)

    # -- 入队 ---------------------------------------------------------------

    def is_enqueued(self, ws_path: str, item_id: int) -> bool:
        # _scheduled 含已 call_soon_threadsafe 但尚未落地的事件，漏看会误报"未入队"
        return (str(ws_path), item_id) in self._active or self._scheduled > 0

    def enqueue(self, db, item_id: int, source_id: int, title: str = "") -> dict | None:
        """线程安全入队；重复任务返回 None。"""
        if self._loop is None:
            raise RuntimeError("archive queue not started")
        self._scheduled += 1
        self._loop.call_soon_threadsafe(self._enqueue, db, item_id, source_id, title)
        return {"state": "pending", "queued": True}

    def _enqueue(self, db, item_id: int, source_id: int, title: str) -> None:
        self._scheduled -= 1
        ws_path = str(db.path)
        key = (ws_path, item_id)
        if key in self._active:
            return
        task = ArchiveTask(
            ws_path=ws_path,
            ws_name=db.path.parent.name,
            db=db,
            item_id=item_id,
            source_id=source_id,
            title=title,
        )
        self._active[key] = task
        self._pending.setdefault((ws_path, source_id), deque()).append(task)
        db.conn.execute(
            "UPDATE items SET fulltext_state = 'pending' WHERE id = ?", (item_id,)
        )
        db.conn.commit()
        self._publish({"type": "archive.queued", **task.view()})
        self._ensure_runner((ws_path, source_id))

    def _ensure_runner(self, key: tuple[str, int]) -> None:
        runner = (self._runners or {}).get(key)
        if runner is not None and not runner.done():
            return
        assert self._loop is not None
        task = self._loop.create_task(self._run_source(key))
        if self._runners is None:
            self._runners = {}
        self._runners[key] = task

    # -- 执行 ---------------------------------------------------------------

    async def _run_source(self, key: tuple[str, int]) -> None:
        while True:
            queue = self._pending.get(key)
            if not queue:
                self._runners.pop(key, None)
                return
            task = queue[0]
            await self._run_one(task)
            queue.popleft()
            self._active.pop((task.ws_path, task.item_id), None)
            self._history.append(task)
            self._dedupe_history(task)

    def _dedupe_history(self, task: ArchiveTask) -> None:
        """同一条目只保留最近一次任务结果。"""
        for i in range(len(self._history) - 1, -1, -1):
            old = self._history[i]
            if old is not task and (old.ws_path, old.item_id) == (
                task.ws_path,
                task.item_id,
            ):
                del self._history[i]

    async def _run_one(self, task: ArchiveTask) -> None:
        task.state = "running"
        self._publish({"type": "archive.started", **task.view()})

        def on_event(info: dict) -> None:
            task.request_index = info.get("request_index", 0)
            task.request_count = info.get("request_count", 0)
            self._publish({"type": "archive.progress", **task.view()})

        try:
            task.result = await fulltext.execute_archive(
                self.core, task.db, task.item_id, on_event
            ) or {}
            task.state = "done"
        except Exception as exc:
            task.state = "failed"
            task.error = str(exc) or exc.__class__.__name__
            log.warning("archive task failed (item=%s): %s", task.item_id, task.error)
        self._publish(
            {"type": f"archive.{task.state}", **task.view()}
        )

    # -- 查询 / 删除广播 ------------------------------------------------------

    def snapshot(self, ws_path: str) -> list[dict]:
        path = str(ws_path)
        running = [t for t in self._active.values() if t.ws_path == path]
        running.sort(key=lambda t: (t.source_id, t.state != "running"))
        history = [t.view() for t in reversed(self._history) if t.ws_path == path]
        return [t.view() for t in running] + history

    def active_count(self, ws_path: str) -> int:
        path = str(ws_path)
        return sum(1 for t in self._active.values() if t.ws_path == path)

    def publish_deleted(self, ws_name: str, item_id: int, source_id: int) -> None:
        self._publish(
            {
                "type": "archive.deleted",
                "item_id": item_id,
                "source_id": source_id,
                "state": "none",
                "workspace": ws_name,
            }
        )

    def publish_uploaded(
        self, ws_name: str, item_id: int, source_id: int, name: str, size: int, kind: str
    ) -> None:
        """用户上传存档完成后的广播：复用 archive.done 语义驱动 UI 全量同步。"""
        self._publish(
            {
                "type": "archive.done",
                "item_id": item_id,
                "source_id": source_id,
                "state": "done",
                "kind": kind,
                "name": name,
                "size": size,
                "workspace": ws_name,
            }
        )

    def purge_history(self, ws_path: str, item_id: int) -> None:
        """删除存档后同步清掉该条目的历史记录，避免面板残留过期 ✓。"""
        path = str(ws_path)
        for i in range(len(self._history) - 1, -1, -1):
            task = self._history[i]
            if task.ws_path == path and task.item_id == item_id:
                del self._history[i]

    def _publish(self, event: dict) -> None:
        self.bus.publish(event)

    # -- 测试辅助 ------------------------------------------------------------

    async def wait_idle(self) -> None:
        while self._scheduled or self._active or any(self._pending.values()):
            await asyncio.sleep(0.005)
