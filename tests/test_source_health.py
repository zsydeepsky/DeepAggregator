import asyncio

import pytest

from app.core.services import source_health
from app.core.services.ingest import fetch_source


class FakeModule:
    def __init__(self, type_, fail_message=None):
        self.type = type_
        self._fail = fail_message
        self.fetched = 0

    async def fetch(self):
        self.fetched += 1
        if self._fail:
            raise RuntimeError(self._fail)
        return []

    async def enrich_items(self, db, source_id):
        pass


def _make_source(db, type_, name):
    cur = db.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES (?, ?, '')", (type_, name)
    )
    db.conn.commit()
    return {"id": cur.lastrowid, "name": name}


def test_classify_credential_errors():
    assert source_health.is_credential_error("reddit", "Reddit credentials are not configured")
    assert source_health.is_credential_error("bilibili", "bilibili api error: -352 风控校验失败")
    assert not source_health.is_credential_error("bilibili", "connection timeout")
    assert not source_health.is_credential_error("rss", "anything")


def test_fetch_failure_reports_and_success_clears(db, monkeypatch):
    source = _make_source(db, "bilibili", "Bilibili UP")
    failing = FakeModule("bilibili", "bilibili api error: -352 风控校验失败")
    monkeypatch.setattr("app.core.services.ingest.get_source", lambda s, a=None: failing)

    with pytest.raises(RuntimeError):
        asyncio.run(fetch_source(db, source))

    snap = source_health.snapshot()
    assert len(snap) == 1
    assert snap[0]["type"] == "bilibili"
    assert snap[0]["sources"] == ["Bilibili UP"]
    assert "-352" in snap[0]["error"]

    # 恢复成功 → 登记清除
    ok = FakeModule("bilibili", None)
    monkeypatch.setattr("app.core.services.ingest.get_source", lambda s, a=None: ok)
    asyncio.run(fetch_source(db, source))
    assert source_health.snapshot() == []


def test_same_type_sources_group_into_one_issue(db, monkeypatch):
    s1 = _make_source(db, "reddit", "r/A")
    s2 = _make_source(db, "reddit", "r/B")
    failing = FakeModule("reddit", "Reddit credentials are not configured")
    monkeypatch.setattr("app.core.services.ingest.get_source", lambda s, a=None: failing)

    for src in (s1, s2):
        with pytest.raises(RuntimeError):
            asyncio.run(fetch_source(db, src))

    snap = source_health.snapshot()
    assert len(snap) == 1  # 同类型合并为一条
    assert set(snap[0]["sources"]) == {"r/A", "r/B"}
    source_health.clear("reddit")
    assert source_health.snapshot() == []
