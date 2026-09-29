from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import settings

USER_AGENT = "Mozilla/5.0 (compatible; DeepAggregator/0.1)"
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


@dataclass(slots=True)
class RawItem:
    guid: str
    title: str
    summary: str
    url: str = ""
    author: str = ""
    published_at: datetime | None = None
    image: str = ""  # 封面/预览图 URL，feed 卡片直接内嵌显示（如 YouTube 缩略图）
    duration: int = 0  # 视频/音频时长（秒）；0 = 未知，-1 = 确认无法获取


@dataclass(slots=True)
class ArchiveRequest:
    url: str
    kind: str = "auto"
    filename: str = ""
    headers: dict = field(default_factory=dict)


def clean_text(value: str, limit: int = 4000) -> str:
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", value or ""))).strip()
    return text[:limit]


async def http_get_text(url: str, params: dict | None = None, headers: dict | None = None) -> str:
    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    ) as client:
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.text
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code not in (406, 429, 500, 503):
                    raise
            except httpx.TransportError as exc:
                last_exc = exc
            if attempt < 3:
                await asyncio.sleep((5, 15, 45)[attempt])
        raise last_exc


class BaseSource:
    type = ""

    def __init__(self, source, app_settings=None):
        self.source = source
        self.app_settings = app_settings
        self.config = json.loads(source["config_json"] or "{}")

    async def fetch(self) -> list[RawItem]:
        raise NotImplementedError

    async def archive_requests(self, item: dict) -> list[ArchiveRequest]:
        return [ArchiveRequest(url=item["url"])]

    def viewer(self, item: dict, assets: list[dict]) -> dict:
        by_kind: dict[str, dict] = {}
        for asset in reversed(assets):
            by_kind.setdefault(asset["kind"], asset)
        for kind in ("html", "json", "md", "pdf", "image", "video", "file"):
            if kind in by_kind:
                return {"kind": kind, "asset": by_kind[kind]}
        return {"kind": "none"}

    async def enrich_items(self, db, source_id: int) -> None:
        """入库后的补充回填钩子（如 YouTube 视频时长），源按需覆写。基类无操作。"""
