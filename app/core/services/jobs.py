import asyncio
import logging
import time

from app.core.services import ingest
from app.core.services.embeddings import embed_missing

log = logging.getLogger("deepaggregator.jobs")

FETCH_SWEEP_SECONDS = 60


class JobManager:
    def __init__(self, core):
        self.core = core
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()
        self._fail_counts: dict[int, int] = {}
        self._next_attempt: dict[int, float] = {}

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        assert self.core.queue is not None
        self.core.queue.start()
        self._spawn(self._recover_archive())
        self._spawn(self._fetch_sweep())
        self._spawn(self._embed_loop())

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.core.queue is not None:
            await self.core.queue.stop()

    def _spawn(self, coro) -> None:
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _recover_archive(self) -> None:
        assert self.core.queue is not None
        try:
            for name in self.core.manager.scan():
                count = self.core.queue.recover(self.core.manager.get(name).db)
                if count:
                    log.info("recovered %d pending archive task(s) in workspace %s", count, name)
        except Exception:
            log.exception("archive recovery failed")

    async def _fetch_sweep(self) -> None:
        while True:
            try:
                db = self.core.db
                due = db.all(
                    "SELECT * FROM sources WHERE enabled = 1"
                    " AND (last_fetched_at IS NULL"
                    " OR (julianday('now') - julianday(last_fetched_at)) * 1440 >= fetch_interval_min)"
                )
                now = time.monotonic()
                for source in due:
                    key = f"{db.path}:{source['id']}"
                    if self._next_attempt.get(key, 0) > now:
                        continue
                    try:
                        result = await ingest.fetch_source(
                            db, source, self.core.app_settings
                        )
                        self._fail_counts.pop(key, None)
                        self._next_attempt.pop(key, None)
                        log.info("fetched source %s: %s", source["name"], result)
                    except Exception as exc:
                        fails = self._fail_counts.get(key, 0) + 1
                        self._fail_counts[key] = fails
                        backoff_min = min(5 * 2 ** (fails - 1), 30)
                        self._next_attempt[key] = (
                            time.monotonic() + backoff_min * 60
                        )
                        log.warning(
                            "source %s failed (%s consecutive), retry in ~%s min: %s",
                            source["name"], fails, backoff_min, exc,
                        )
                    await asyncio.sleep(3)
            except Exception:
                log.exception("fetch sweep failed")
            await asyncio.sleep(FETCH_SWEEP_SECONDS)

    async def _embed_loop(self) -> None:
        while True:
            try:
                count = await embed_missing(
                    self.core.db,
                    self.core.embedder,
                    self.core.settings.embed_batch_size,
                )
            except Exception:
                log.exception("embed loop failed")
                count = 0
            await asyncio.sleep(1 if count else self.core.settings.embed_poll_seconds)
