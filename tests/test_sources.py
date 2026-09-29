import asyncio

import pytest

from app.core.sources import get_source


def make_source_row(type_: str) -> dict:
    return {"type": type_, "url": "", "config_json": "{}"}


def test_arxiv_archive_plan_prefers_pdf_with_html_fallback():
    module = get_source(make_source_row("arxiv"))
    requests = asyncio.run(
        module.archive_requests({"url": "http://arxiv.org/abs/2401.00001v1"})
    )
    assert [r.kind for r in requests] == ["pdf", "html"]
    assert requests[0].url == "https://arxiv.org/pdf/2401.00001v1"
    assert requests[0].filename == "2401.00001v1.pdf"
    assert requests[1].filename == "2401.00001v1.html"
    assert requests[1].url == "http://arxiv.org/abs/2401.00001v1"


def test_arxiv_viewer_prefers_pdf_then_html_then_none():
    module = get_source(make_source_row("arxiv"))
    assets = [{"kind": "html", "id": 5}, {"kind": "pdf", "id": 6}]
    view = module.viewer({}, assets)
    assert view["kind"] == "pdf"
    assert view["asset"]["id"] == 6
    assert module.viewer({}, [{"kind": "html", "id": 5}])["kind"] == "html"
    assert module.viewer({}, [])["kind"] == "none"


def test_rss_viewer_defaults_to_html():
    module = get_source(make_source_row("rss"))
    assert module.viewer({}, [{"kind": "html", "id": 3}])["kind"] == "html"
    assert module.viewer({}, [])["kind"] == "none"


def test_registry_rejects_unknown_type():
    with pytest.raises(KeyError):
        get_source({"type": "mastodon", "config_json": "{}"})
