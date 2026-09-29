import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.db import Database
from app.core.services.ingest import upsert_items
from app.core.sources import get_source
from app.core.sources.base import RawItem
from app.core.sources.youtube import parse_feed, resolve_channel_id
from test_archive_queue import make_core

YT_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>abc12345678</yt:videoId>
    <yt:channelId>UCbCyMcfqds7slfOU_RgDita</yt:channelId>
    <title>Opening Keynote</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc12345678"/>
    <published>2026-09-01T10:00:00+00:00</published>
    <media:group>
      <media:title>Opening Keynote</media:title>
      <media:thumbnail url="https://i4.ytimg.com/vi/abc12345678/hqdefault.jpg" width="480" height="360"/>
      <media:description>Full description text here</media:description>
    </media:group>
    <author><name>GDC</name></author>
  </entry>
  <entry>
    <yt:videoId></yt:videoId>
    <title>broken entry without id</title>
  </entry>
</feed>
"""


def test_parse_feed_builds_raw_items_with_thumbnail():
    items = parse_feed(YT_XML)
    assert len(items) == 1
    item = items[0]
    assert item.guid == "yt_abc12345678"
    assert item.title == "Opening Keynote"
    assert item.summary == "Full description text here"
    assert item.image == "https://i4.ytimg.com/vi/abc12345678/hqdefault.jpg"
    assert item.url == "https://www.youtube.com/watch?v=abc12345678"
    assert item.author == "GDC"
    assert item.published_at is not None


def test_resolve_channel_id():
    assert resolve_channel_id("UCXuqSBlHAE6Xw-yeJA0Tunw") == "UCXuqSBlHAE6Xw-yeJA0Tunw"
    assert (
        resolve_channel_id("https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw")
        == "UCXuqSBlHAE6Xw-yeJA0Tunw"
    )
    html = '<link rel="canonical" href="https://www.youtube.com/channel/UCbCyMcfqds7slfOU_RgDita">'
    assert resolve_channel_id("@GDCFestivalofGaming", html) == "UCbCyMcfqds7slfOU_RgDita"
    with pytest.raises(ValueError):
        resolve_channel_id("@no-such-handle", "<html><body>consent</body></html>")


def test_resolve_channel_id_prefers_external_id_over_stray_uc():
    # 页面前部可能出现其它频道的裸 UC id（推荐位），externalId 必须优先
    html = (
        '"channelId":"UClZTEZ_x4itdCYcpJBYF1E1",'
        '"externalId":"UC0JB7TSe49lg56u6qH8y_MQ"'
    )
    assert resolve_channel_id("@GDCFestivalofGaming", html) == "UC0JB7TSe49lg56u6qH8y_MQ"
    canonical_only = (
        '<link rel="canonical" href="https://www.youtube.com/channel/UCbCyMcfqds7slfOU_RgDita">'
        '"featuredChannel":"UCXuqSBlHAE6Xw-yeJA0Tunw"'
    )
    assert resolve_channel_id("@somehandle", canonical_only) == "UCbCyMcfqds7slfOU_RgDita"


def test_enrich_items_backfills_duration(db, monkeypatch):
    from app.core.sources import get_source

    db.conn.execute("INSERT INTO sources(type, name, url) VALUES ('youtube', 'gdc', '')")
    db.conn.execute(
        "INSERT INTO items(source_id, guid_hash, title, url, duration)"
        " VALUES (1, 'h1', 'a', 'https://www.youtube.com/watch?v=aaa', 0)"
    )
    db.conn.execute(
        "INSERT INTO items(source_id, guid_hash, title, url, duration)"
        " VALUES (1, 'h2', 'b', 'https://www.youtube.com/watch?v=bbb', 0)"
    )
    db.conn.commit()

    async def fake_http_get_text(url, params=None):
        if url.endswith("v=aaa"):
            return '<script>"lengthSeconds":"754"</script>'
        return "<html><body>no length here</body></html>"

    monkeypatch.setattr("app.core.sources.youtube.http_get_text", fake_http_get_text)
    module = get_source({"type": "youtube", "url": "", "config_json": "{}"})
    asyncio.run(module.enrich_items(db, 1))
    assert db.one("SELECT duration FROM items WHERE guid_hash='h1'")["duration"] == 754
    assert db.one("SELECT duration FROM items WHERE guid_hash='h2'")["duration"] == -1


def test_youtube_archive_plan_is_html_fallback():
    module = get_source({"type": "youtube", "url": "", "config_json": "{}"})
    requests = asyncio.run(
        module.archive_requests({"url": "https://www.youtube.com/watch?v=x"})
    )
    assert [r.kind for r in requests] == ["html"]


def test_youtube_viewer_prefers_uploaded_media_over_html():
    module = get_source({"type": "youtube", "url": "", "config_json": "{}"})
    assets = [{"kind": "html", "id": 1}, {"kind": "video", "id": 2}]
    view = module.viewer({}, assets)
    assert view["kind"] == "video" and view["asset"]["id"] == 2
    assert module.viewer({}, [{"kind": "html", "id": 1}])["kind"] == "html"
    assert module.viewer({}, [])["kind"] == "none"


def test_ingest_stores_image(db):
    db.conn.execute("INSERT INTO sources(type, name, url) VALUES ('youtube', 'gdc', '')")
    db.conn.commit()
    items = [
        RawItem(guid="yt_x", title="t", summary="s", url="u", image="https://img/x.jpg")
    ]
    assert upsert_items(db, 1, items) == 1
    assert db.one("SELECT image FROM items")["image"] == "https://img/x.jpg"


OLD_SOURCES_SCHEMA = """
CREATE TABLE sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK(type IN ('rss', 'arxiv', 'reddit')),
  name TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  config_json TEXT NOT NULL DEFAULT '{}',
  fetch_interval_min INTEGER NOT NULL DEFAULT 60,
  enabled INTEGER NOT NULL DEFAULT 1,
  color TEXT NOT NULL DEFAULT '',
  archive_enabled INTEGER NOT NULL DEFAULT 1,
  archive_markdown INTEGER NOT NULL DEFAULT 0,
  last_fetched_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def test_migration_opens_youtube_type_and_new_columns(tmp_path):
    db_path = tmp_path / "old" / "aggregator.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.executescript(OLD_SOURCES_SCHEMA)
    conn.execute(
        "INSERT INTO sources(type, name, url, archive_markdown)"
        " VALUES ('rss', 'legacy', 'https://x.com/feed', 1)"
    )
    conn.commit()
    conn.close()

    database = Database(db_path)
    database.init()
    c = database.conn
    cur = c.execute(
        "INSERT INTO sources(type, name, url, allow_upload) VALUES ('youtube', 'gdc', '', 1)"
    )
    assert cur.rowcount == 1
    c.execute(
        "INSERT INTO items(source_id, guid_hash, title, image)"
        " VALUES (1, 'h1', 't', 'https://img/y.jpg')"
    )
    c.commit()
    kept = c.execute(
        "SELECT name, archive_markdown FROM sources WHERE type = 'rss'"
    ).fetchone()
    assert kept["name"] == "legacy" and kept["archive_markdown"] == 1
    assert c.execute("SELECT allow_upload FROM sources WHERE type='youtube'").fetchone()[
        "allow_upload"
    ] == 1
    assert c.execute("SELECT image FROM items").fetchone()["image"] == "https://img/y.jpg"
    assert c.execute("SELECT sort_order FROM sources").fetchone()["sort_order"] == 0
    c.execute("INSERT INTO ui_prefs(key, value) VALUES ('k', 'v')")
    c.commit()
    assert c.execute("SELECT value FROM ui_prefs WHERE key='k'").fetchone()["value"] == "v"
    c.execute(
        "INSERT INTO sources(type, name, url) VALUES ('bilibili', 'bili', '')"
    )
    c.commit()
    assert (
        c.execute("SELECT COUNT(*) c FROM sources WHERE type='bilibili'").fetchone()["c"]
        == 1
    )


def test_source_prefs_roundtrip(tmp_path):
    core = make_core(tmp_path)
    db = core.db
    db.conn.execute(
        "INSERT INTO sources(type, name, url, sort_order) VALUES ('rss', 'a', '', 5)"
    )
    db.conn.commit()
    assert core.get_source_prefs() == {"group_order": []}
    core.set_source_prefs(["youtube", "rss"], [1])
    assert core.get_source_prefs() == {"group_order": ["youtube", "rss"]}
    assert db.one("SELECT sort_order FROM sources WHERE id = 1")["sort_order"] == 0


def _make_youtube_item(core, allow_upload: int) -> int:
    db = core.db
    cur = db.conn.execute(
        "INSERT INTO sources(type, name, url, allow_upload) VALUES ('youtube', 'gdc', '', ?)",
        (allow_upload,),
    )
    sid = cur.lastrowid
    cur = db.conn.execute(
        "INSERT INTO items(source_id, guid_hash, title, url) VALUES (?, 'g1', 'talk', 'https://youtu.be/x')",
        (sid,),
    )
    db.conn.commit()
    return cur.lastrowid


def test_upload_archive_roundtrip_and_overwrite(tmp_path):
    core = make_core(tmp_path)
    core.settings = SimpleNamespace(archive_max_bytes=100 * 1024 * 1024)
    iid = _make_youtube_item(core, allow_upload=1)

    f1 = tmp_path / "my talk.mp4"
    f1.write_bytes(b"0" * 1024)
    item = core.upload_archive(iid, "my talk.mp4", "video/mp4", f1)
    assert item["fulltext_state"] == "done"
    asset = item["assets"][0]
    assert asset["kind"] == "video" and asset["name"] == "my talk.mp4"
    dest1 = core.manager.current().path / "assets" / "youtube" / "my talk.mp4"
    assert dest1.exists()

    f2 = tmp_path / "other.mp4"
    f2.write_bytes(b"1" * 10)
    item2 = core.upload_archive(iid, "other.mp4", "video/mp4", f2)
    assert len(item2["assets"]) == 1
    assert item2["assets"][0]["name"] == "other.mp4"
    assert not dest1.exists()


def test_upload_rejected_without_allow_upload(tmp_path):
    core = make_core(tmp_path)
    core.settings = SimpleNamespace(archive_max_bytes=100 * 1024 * 1024)
    iid = _make_youtube_item(core, allow_upload=0)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    with pytest.raises(PermissionError):
        core.upload_archive(iid, "a.mp4", "video/mp4", f)


def test_upload_rejects_oversize(tmp_path):
    core = make_core(tmp_path)
    core.settings = SimpleNamespace(archive_max_bytes=100)
    iid = _make_youtube_item(core, allow_upload=1)
    f = tmp_path / "big.mp4"
    f.write_bytes(b"0" * 200)
    with pytest.raises(ValueError):
        core.upload_archive(iid, "big.mp4", "video/mp4", f)
