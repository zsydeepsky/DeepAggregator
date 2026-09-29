"""YouTube 频道源：频道 RSS 抓取（免 API key），feed 卡片显示 简介+封面图。

config: {"channel": "@handle / 频道 URL / UC 频道 id"}
抓取：解析频道 id（@handle 需抓一次频道页，进程内缓存）→
      https://www.youtube.com/feeds/videos.xml?channel_id=<id>（最新 15 条）
条目：RawItem(guid="yt_<videoId>", image=缩略图, ...)，卡片内嵌封面图。
存档：watch 页快照仅作兜底；视频本体原则上由用户经「上传存档」提供（allow_upload）。
viewer：优先用户上传的 video/image 资产，html 快照殿后。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from app.core.sources.base import ArchiveRequest, BaseSource, RawItem, http_get_text

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_LENGTH_SECONDS = re.compile(r'"lengthSeconds":"(\d+)"')
# 页面解析优先级：externalId 是频道自身 id；canonical 次之；裸 UC 匹配兜底（可能误中推荐频道）
_EXTERNAL_ID = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')
_CANONICAL = re.compile(r'href="[^"]*?/channel/(UC[A-Za-z0-9_-]{22})"')

CHANNEL_CACHE: dict[str, str] = {}


def _direct_channel_id(value: str) -> str:
    m = _CHANNEL_ID.search(value or "")
    return m.group(0) if m else ""


def _channel_id_from_html(html: str) -> str:
    for pattern in (_EXTERNAL_ID, _CANONICAL):
        m = pattern.search(html or "")
        if m:
            return m.group(1)
    return _direct_channel_id(html or "")


def resolve_channel_id(channel: str, page_html: str = "") -> str:
    """从频道值（UC id / 频道 URL / @handle 页面 HTML）提取 UC 频道 id。"""
    direct = _direct_channel_id(channel)
    if direct:
        return direct
    from_html = _channel_id_from_html(page_html)
    if from_html:
        return from_html
    raise ValueError(f"cannot resolve youtube channel id from {channel!r}")


def parse_feed(xml_text: str) -> list[RawItem]:
    root = ET.fromstring(xml_text)
    items: list[RawItem] = []
    for entry in root.findall("atom:entry", _NS):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=_NS) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=_NS) or "").strip()
        if not video_id or not title:
            continue
        description = ""
        thumbnail = ""
        group = entry.find("media:group", _NS)
        if group is not None:
            description = (
                group.findtext("media:description", default="", namespaces=_NS) or ""
            ).strip()
            thumb = group.find("media:thumbnail", _NS)
            if thumb is not None:
                thumbnail = thumb.get("url", "")
        if len(description) > 400:
            description = description[:400] + "..."
        published = None
        published_raw = (entry.findtext("atom:published", default="", namespaces=_NS) or "").strip()
        if published_raw:
            try:
                published = datetime.fromisoformat(published_raw)
            except ValueError:
                published = None
        link = entry.find("atom:link", _NS)
        url = (link.get("href") if link is not None else "") or (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        author_el = entry.find("atom:author/atom:name", _NS)
        items.append(
            RawItem(
                guid=f"yt_{video_id}",
                title=title,
                summary=description or "(视频)",
                url=url,
                author=(author_el.text or "").strip() if author_el is not None else "",
                published_at=published,
                image=thumbnail,
            )
        )
    return items


class YouTubeSource(BaseSource):
    type = "youtube"

    def channel_value(self) -> str:
        return (
            self.config.get("channel_id")
            or self.config.get("channel")
            or self.source["url"]
            or ""
        ).strip()

    async def _channel_id(self) -> str:
        value = self.channel_value()
        if not value:
            raise ValueError("youtube source requires a channel")
        cached = CHANNEL_CACHE.get(value)
        if cached:
            return cached
        direct = _direct_channel_id(value)
        if not direct:
            page_url = value if value.startswith("http") else f"https://www.youtube.com/{value}"
            html = await http_get_text(page_url)
            direct = resolve_channel_id(value, html)
        CHANNEL_CACHE[value] = direct
        return direct

    async def fetch(self) -> list[RawItem]:
        channel_id = await self._channel_id()
        xml_text = await http_get_text(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        )
        return parse_feed(xml_text)

    async def archive_requests(self, item: dict) -> list[ArchiveRequest]:
        # 视频本体无法直接抓取；watch 页快照仅作兜底，正式内容由用户「上传存档」提供
        return [ArchiveRequest(url=item.get("url") or "", kind="html")]

    async def enrich_items(self, db, source_id: int) -> None:
        """回填视频时长：抓 watch 页提取 lengthSeconds。每次至多 10 条，失败下轮再试；
        页面里确实找不到时长的记 -1（永久跳过，避免反复抓）。"""
        rows = db.all(
            "SELECT id, url FROM items WHERE source_id = ? AND duration = 0 AND url != ''"
            " ORDER BY id DESC LIMIT 10",
            (source_id,),
        )
        for row in rows:
            try:
                html = await http_get_text(row["url"])
            except Exception:
                break  # 网络问题：停下，留给下一轮
            m = _LENGTH_SECONDS.search(html)
            seconds = int(m.group(1)) if m else -1
            db.conn.execute(
                "UPDATE items SET duration = ? WHERE id = ?", (seconds, row["id"])
            )
            db.conn.commit()

    def viewer(self, item: dict, assets: list[dict]) -> dict:
        by_kind: dict[str, dict] = {}
        for asset in reversed(assets):
            by_kind.setdefault(asset["kind"], asset)
        for kind in ("video", "image", "pdf", "json", "md", "file", "html"):
            if kind in by_kind:
                return {"kind": kind, "asset": by_kind[kind]}
        return {"kind": "none"}
