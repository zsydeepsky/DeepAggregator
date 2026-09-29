import asyncio
import pytest

from app.core.sources import get_source
from app.core.sources.bilibili import (
    mixin_wbi_key,
    parse_length,
    parse_space_videos,
    resolve_mid,
    wbi_sign,
)

# bilibili-API-collect 公开的 wbi 混排置换表（测试内嵌为规格基准）
_WBI_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def test_mixin_wbi_key_applies_documented_table():
    combined = "0123456789abcdef" * 4  # 64 位可区分字符
    expected = "".join(combined[i] for i in _WBI_TABLE)[:32]
    assert mixin_wbi_key(combined) == expected
    assert len(mixin_wbi_key(combined)) == 32


def test_resolve_mid():
    assert resolve_mid("https://space.bilibili.com/35868098") == "35868098"
    assert resolve_mid("35868098") == "35868098"
    html = '<script>window.__INITIAL_STATE__={"up":{"mid":35868098}};</script>'
    assert resolve_mid("@some-author", html) == "35868098"
    with pytest.raises(ValueError):
        resolve_mid("no digits", "<html></html>")


def test_parse_length():
    assert parse_length("12:34") == 754
    assert parse_length("1:02:03") == 3723
    assert parse_length("") == 0


def test_parse_space_videos():
    payload = {
        "code": 0,
        "data": {
            "list": {
                "vlist": [
                    {
                        "bvid": "BV1xx411c7mD",
                        "title": "Demo video",
                        "description": "desc text",
                        "length": "12:34",
                        "created": 1700000000,
                        "pic": "http://i0.hdslb.com/bfs/archive/x.jpg",
                        "author": "uploader",
                    },
                    {"title": "entry without bvid"},
                ]
            }
        },
    }
    items = parse_space_videos(payload)
    assert len(items) == 1
    it = items[0]
    assert it.guid == "bili_BV1xx411c7mD"
    assert it.url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert it.image == "https://i0.hdslb.com/bfs/archive/x.jpg"
    assert it.duration == 754
    assert it.author == "uploader"
    assert it.published_at is not None


def test_parse_space_videos_raises_on_risk_control():
    with pytest.raises(RuntimeError):
        parse_space_videos({"code": -352, "message": "请求被拦截"})


def test_wbi_sign_structure():
    signed = wbi_sign({"mid": 35868098, "order": "pubdate"}, "k" * 32)
    assert signed["mid"] == "35868098"
    assert str(signed["wts"]).isdigit()
    assert len(signed["w_rid"]) == 32
    # 排序键参与签名：参数保持完整
    assert set(signed) == {"mid", "order", "wts", "w_rid"}


def test_bilibili_archive_plan_and_viewer():
    module = get_source({"type": "bilibili", "url": "", "config_json": "{}"})
    requests = asyncio.run(
        module.archive_requests({"url": "https://www.bilibili.com/video/BV1x"})
    )
    assert [r.kind for r in requests] == ["html"]
    assert module.viewer({}, [{"kind": "html", "id": 1}])["kind"] == "html"
    assert module.viewer({}, [{"kind": "video", "id": 2}, {"kind": "html", "id": 1}])[
        "kind"
    ] == "video"
    assert module.viewer({}, [])["kind"] == "none"
