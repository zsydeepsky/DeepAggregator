import asyncio

import pytest

from app.core.api import CoreAPI
from app.core.services import fulltext
from app.core.services.archive_queue import ArchiveQueue, ArchiveTask
from app.core.workspaces import WorkspaceManager
from conftest import FakeEmbedder


class Recorder:
    """假存档执行器：记录启动顺序与最大并发数。"""

    def __init__(self, delay=0.01):
        self.order = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.delay = delay

    async def __call__(self, core, db, item_id, on_event=None):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.order.append(("start", item_id))
        await asyncio.sleep(self.delay)
        self.order.append(("end", item_id))
        self.concurrent -= 1
        return {"kind": "html", "name": f"{item_id}.html", "size": 10, "mime": "text/html"}


def make_source(db, name="a"):
    cur = db.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES ('rss', ?, '')", (name,)
    )
    db.conn.commit()
    return cur.lastrowid


def make_items(db, source_id, count):
    ids = []
    for i in range(count):
        cur = db.conn.execute(
            "INSERT INTO items(source_id, guid_hash, title, url) VALUES (?, ?, ?, ?)",
            (source_id, f"g{source_id}-{i}", f"item {source_id}-{i}", "https://x.com/a"),
        )
        ids.append(cur.lastrowid)
    db.conn.commit()
    return ids


def run_queue(db, core, jobs):
    async def main():
        queue = ArchiveQueue(core)
        queue.start()
        for item_id, source_id in jobs:
            queue.enqueue(db, item_id, source_id, "t")
        await queue.wait_idle()
        await queue.stop()
        return queue

    return asyncio.run(main())


def test_queue_serializes_same_source_fifo(db, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    sid = make_source(db)
    ids = make_items(db, sid, 3)

    run_queue(db, object(), [(iid, sid) for iid in ids])

    assert rec.max_concurrent == 1
    starts = [iid for op, iid in rec.order if op == "start"]
    assert starts == ids


def test_queue_runs_different_sources_in_parallel(db, monkeypatch):
    rec = Recorder(delay=0.05)
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    sid_a = make_source(db, "a")
    sid_b = make_source(db, "b")
    id_a = make_items(db, sid_a, 1)[0]
    id_b = make_items(db, sid_b, 1)[0]

    run_queue(db, object(), [(id_a, sid_a), (id_b, sid_b)])

    assert rec.max_concurrent == 2


def test_queue_dedupes_same_item(db, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    sid = make_source(db)
    iid = make_items(db, sid, 1)[0]

    async def main():
        queue = ArchiveQueue(object())
        queue.start()
        queue.enqueue(db, iid, sid, "t")
        await asyncio.sleep(0)  # 让入队回调先落地
        queue.enqueue(db, iid, sid, "t")
        await queue.wait_idle()
        await queue.stop()

    asyncio.run(main())
    assert rec.order.count(("start", iid)) == 1


def test_queue_marks_pending_and_publishes_events(db, monkeypatch):
    rec = Recorder(delay=0)
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    sid = make_source(db)
    iid = make_items(db, sid, 1)[0]

    async def main():
        queue = ArchiveQueue(object())
        queue.start()
        sub = queue.bus.subscribe()
        queue.enqueue(db, iid, sid, "标题")
        await queue.wait_idle()
        events = []
        while not sub.empty():
            events.append(sub.get_nowait())
        snap = queue.snapshot(str(db.path))
        await queue.stop()
        return events, snap

    events, snap = asyncio.run(main())
    assert [e["type"] for e in events] == [
        "archive.queued",
        "archive.started",
        "archive.done",
    ]
    assert all(e["item_id"] == iid for e in events)
    assert events[0]["workspace"] == db.path.parent.name
    assert db.one("SELECT fulltext_state FROM items WHERE id = ?", (iid,))[
        "fulltext_state"
    ] == "pending"
    assert len(snap) == 1 and snap[0]["state"] == "done"


def test_queue_recovers_pending_items(db, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    sid = make_source(db)
    iid = make_items(db, sid, 1)[0]
    db.conn.execute("UPDATE items SET fulltext_state = 'pending' WHERE id = ?", (iid,))
    db.conn.commit()

    async def main():
        queue = ArchiveQueue(object())
        queue.start()
        assert queue.recover(db) == 1
        await queue.wait_idle()
        await queue.stop()

    asyncio.run(main())
    assert rec.order.count(("start", iid)) == 1


def make_core(tmp_path):
    manager = WorkspaceManager(tmp_path)
    core = CoreAPI(manager, None, FakeEmbedder(), None)
    core.queue = ArchiveQueue(core)
    return core


def make_asset_fixture(core):
    db = core.db
    sid = make_source(db)
    cur = db.conn.execute(
        "INSERT INTO items(source_id, guid_hash, title, url, fulltext_state)"
        " VALUES (?, 'g1', 't', 'https://x.com/a', 'done')",
        (sid,),
    )
    item_id = cur.lastrowid
    assets_dir = core.manager.current().path / "assets" / "rss"
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / "a.html"
    target.write_bytes(b"<p>hi</p>")
    db.conn.execute(
        "INSERT INTO assets(item_id, kind, mime, size, sha256, path, name)"
        " VALUES (?, 'html', 'text/html', 9, 'deadbeef', 'rss/a.html', 'a.html')",
        (item_id,),
    )
    db.conn.commit()
    return item_id, target


def test_delete_archive_removes_files_rows_and_resets_state(tmp_path):
    core = make_core(tmp_path)
    item_id, target = make_asset_fixture(core)

    item = core.delete_archive(item_id)

    assert item["fulltext_state"] == "none"
    assert item["assets"] == []
    assert not target.exists()
    assert core.db.one("SELECT 1 FROM assets WHERE item_id = ?", (item_id,)) is None


def test_delete_archive_rejected_while_enqueued(tmp_path):
    core = make_core(tmp_path)
    item_id, target = make_asset_fixture(core)
    db_path = str(core.db.path)
    core.queue._active[(db_path, item_id)] = ArchiveTask(
        ws_path=db_path, ws_name="default", db=core.db, item_id=item_id, source_id=1
    )

    with pytest.raises(RuntimeError):
        core.delete_archive(item_id)
    assert target.exists()
    assert core.db.one("SELECT 1 FROM assets WHERE item_id = ?", (item_id,))


def test_delete_archive_purges_queue_history(tmp_path, monkeypatch):
    rec = Recorder(delay=0)
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    core = make_core(tmp_path)
    db = core.db
    sid = make_source(db)
    iid = make_items(db, sid, 1)[0]

    async def main():
        core.queue.start()
        core.request_archive(iid)
        await core.queue.wait_idle()
        assert len(core.queue.snapshot(str(db.path))) == 1
        core.delete_archive(iid)
        snap = core.queue.snapshot(str(db.path))
        await core.queue.stop()
        return snap

    snap = asyncio.run(main())
    assert snap == []


def test_request_archive_enqueues_and_dedupes(tmp_path, monkeypatch):
    rec = Recorder(delay=0.02)
    monkeypatch.setattr(fulltext, "execute_archive", rec)
    core = make_core(tmp_path)
    db = core.db
    sid = make_source(db)
    iid = make_items(db, sid, 1)[0]

    async def main():
        core.queue.start()
        first = core.request_archive(iid)
        await asyncio.sleep(0)  # 让入队回调落地（含 pending 状态回写）
        second = core.request_archive(iid)
        await core.queue.wait_idle()
        await core.queue.stop()
        return first, second

    first, second = asyncio.run(main())
    assert first["queued"] is True
    assert second["queued"] is False
    assert rec.order.count(("start", iid)) == 1
