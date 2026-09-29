import asyncio
import time

from app.core.sources.reddit import TOKEN_CACHE, RedditSource, parse_children


class FakeAppSettings:
    def __init__(self, data):
        self.data = data


def make_reddit_source(config: str) -> RedditSource:
    return RedditSource(
        {"type": "reddit", "url": "", "config_json": config},
        app_settings=FakeAppSettings(
            {"reddit": {"client_id": "test-id", "client_secret": "test-secret"}}
        ),
    )


def test_subreddits_normalization():
    src = make_reddit_source('{"subreddits": ["r/LocalLLaMA", "Unity", " r/unity "]}')
    assert src.subreddits() == ["localllama", "unity"]


def test_parse_children_builds_raw_items():
    children = [
        {
            "kind": "t3",
            "data": {
                "id": "abc123",
                "name": "t3_abc123",
                "title": "New model release",
                "selftext": "x" * 500,
                "permalink": "/r/localllama/comments/abc123/new_model_release/",
                "author": "someone",
                "created_utc": 1700000000.0,
            },
        },
        {"kind": "t1", "data": {"body": "comment"}},
    ]
    items = parse_children(children)
    assert len(items) == 1
    item = items[0]
    assert item.guid == "t3_abc123"
    assert item.title == "New model release"
    assert item.summary.endswith("...")
    assert item.url == "https://www.reddit.com/r/localllama/comments/abc123/new_model_release/"
    assert item.published_at is not None


def test_parse_children_extracts_preview_and_duration():
    children = [
        {
            "kind": "t3",
            "data": {
                "id": "vid1",
                "name": "t3_vid1",
                "title": "Video post",
                "permalink": "/r/unity/comments/vid1/video_post/",
                "media": {"reddit_video": {"duration": 42, "fallback_url": "https://v.redd.it/x"}},
                "preview": {
                    "images": [
                        {
                            "source": {"url": "https://preview.redd.it/big.png", "width": 1200},
                            "resolutions": [
                                {"url": "https://preview.redd.it/small.png", "width": 216},
                                {"url": "https://preview.redd.it/mid.png&amp;width=360", "width": 360},
                            ],
                        }
                    ]
                },
            },
        },
        {
            "kind": "t3",
            "data": {"id": "txt1", "name": "t3_txt1", "title": "Text only", "permalink": "/r/x/comments/txt1/t/"},
        },
    ]
    items = parse_children(children)
    video_item = items[0]
    assert video_item.image == "https://preview.redd.it/mid.png&width=360"
    assert video_item.duration == 42
    text_item = items[1]
    assert text_item.image == ""
    assert text_item.duration == 0


def test_enrich_items_backfills_image_and_duration(db, monkeypatch):
    from app.core.sources.reddit import TOKEN_CACHE

    db.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES ('reddit', 'r/test', '')"
    )
    db.conn.execute(
        "INSERT INTO items(source_id, guid_hash, title, url, guid)"
        " VALUES (1, 'h1', 'a', 'https://www.reddit.com/r/x/1', 't3_post1')"
    )
    db.conn.execute(
        "INSERT INTO items(source_id, guid_hash, title, url, guid)"
        " VALUES (1, 'h2', 'b', 'https://www.reddit.com/r/x/2', 't3_post2')"
    )
    db.conn.commit()
    TOKEN_CACHE[("test-id", "test-secret")] = ("token-xyz", time.time() + 3600)

    class FakeResp:
        def raise_for_status(self):
            pass

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    payloads = {
        "post1": [
            {
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "preview": {
                                    "images": [
                                        {
                                            "source": {"url": "https://p.redd.it/a.png", "width": 900},
                                            "resolutions": [
                                                {"url": "https://p.redd.it/a360.png", "width": 360}
                                            ],
                                        }
                                    ]
                                },
                                "media": {"reddit_video": {"duration": 30}},
                            },
                        }
                    ]
                }
            },
            {"data": {"children": []}},
        ],
        "post2": [{"data": {"children": [{"kind": "t3", "data": {"id": "post2"}}]}}],
    }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            key = "post1" if "post1" in url else "post2"
            return FakeResp(payloads[key])

    monkeypatch.setattr(
        "app.core.sources.reddit.httpx.AsyncClient", FakeClient
    )
    from app.core.sources.reddit import RedditSource

    src = RedditSource(
        {"type": "reddit", "url": "", "config_json": "{}"},
        app_settings=FakeAppSettings(
            {"reddit": {"client_id": "test-id", "client_secret": "test-secret"}}
        ),
    )
    asyncio.run(src.enrich_items(db, 1))
    first = db.one("SELECT image, duration FROM items WHERE guid_hash='h1'")
    assert first["image"] == "https://p.redd.it/a360.png"
    assert first["duration"] == 30
    second = db.one("SELECT image, duration FROM items WHERE guid_hash='h2'")
    assert second["image"] == "-1"  # 确认无图，不再重试
    assert second["duration"] == 0


def test_archive_plan_json_first_with_html_fallback():
    src = make_reddit_source('{"subreddits": ["localllama"]}')
    TOKEN_CACHE[("test-id", "test-secret")] = ("token-xyz", time.time() + 3600)
    requests = asyncio.run(src.archive_requests({"guid": "t3_abc123", "url": ""}))
    assert [r.kind for r in requests] == ["json", "html"]
    assert requests[0].url == (
        "https://oauth.reddit.com/comments/abc123?limit=20&sort=hot&raw_json=1"
    )
    assert requests[0].filename == "abc123.json"
    assert requests[0].headers["Authorization"] == "Bearer token-xyz"
    assert requests[1].kind == "html"
