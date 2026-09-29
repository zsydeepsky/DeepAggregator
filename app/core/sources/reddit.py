from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx

from app.core.sources.base import USER_AGENT, ArchiveRequest, BaseSource, RawItem

log = logging.getLogger("deepaggregator.reddit")
TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_DIRECT_IMAGE = re.compile(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", re.I)


def _preview_url(d: dict) -> str:
    """feed 卡片缩略图：优先中等分辨率预览图，其次直链图片；纯文字帖返回空。"""
    images = ((d.get("preview") or {}).get("images")) or []
    if not images:
        dest = d.get("url_overridden_by_dest") or ""
        return dest if _DIRECT_IMAGE.search(dest) else ""
    img = images[0]
    best = None
    for r in img.get("resolutions") or []:
        w = r.get("width") or 0
        if 200 <= w <= 720:
            best = r
            if w >= 360:
                break
    url = (best or img.get("source") or {}).get("url") or ""
    return url.replace("&amp;", "&")


async def get_token(app_settings) -> str:
    cfg = (app_settings.data.get("reddit") if app_settings else {}) or {}
    client_id = cfg.get("client_id", "")
    client_secret = cfg.get("client_secret", "")
    if not client_id or not client_secret:
        raise RuntimeError("Reddit credentials are not configured")
    key = (client_id, client_secret)
    cached = TOKEN_CACHE.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    token = data["access_token"]
    expires = time.time() + float(data.get("expires_in", 3600))
    TOKEN_CACHE[key] = (token, expires)
    return token


def parse_children(children: list[dict]) -> list[RawItem]:
    items: list[RawItem] = []
    for child in children:
        if child.get("kind") != "t3":
            continue
        d = child.get("data", {})
        title = (d.get("title") or "").strip()
        if not title:
            continue
        summary = (d.get("selftext") or "").strip()
        if len(summary) > 400:
            summary = summary[:400] + "..."
        post_id = d.get("id") or ""
        permalink = d.get("permalink") or f"/r/{d.get('subreddit', 'reddit')}/comments/{post_id}/"
        video_duration = int(
            ((d.get("media") or {}).get("reddit_video") or {}).get("duration") or 0
        )
        items.append(
            RawItem(
                guid=d.get("name") or f"t3_{post_id}",
                title=title,
                summary=summary or "(link post)",
                url="https://www.reddit.com" + permalink,
                author=d.get("author") or "",
                published_at=datetime.fromtimestamp(
                    float(d.get("created_utc", 0)), tz=timezone.utc
                ),
                image=_preview_url(d),
                duration=video_duration,
            )
        )
    return items


class RedditSource(BaseSource):
    type = "reddit"

    def subreddits(self) -> list[str]:
        raw = self.config.get("subreddits") or []
        if isinstance(raw, str):
            raw = [p for p in raw.split(",")]
        subs: list[str] = []
        for part in raw:
            name = str(part).strip().lower().removeprefix("r/").strip("/")
            if name and name not in subs:
                subs.append(name)
        return subs

    async def fetch(self) -> list[RawItem]:
        subs = self.subreddits()
        if not subs:
            return []
        token = await get_token(self.app_settings)
        limit = int(self.config.get("limit", 25))
        url = f"https://oauth.reddit.com/r/{'+'.join(subs)}/new?limit={limit}&raw_json=1"
        async with httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            children = resp.json().get("data", {}).get("children", [])
        return parse_children(children)

    async def enrich_items(self, db, source_id: int) -> None:
        """回填存量条目的缩略图与视频时长：逐条取 /comments/<id>.json。
        每轮至多 10 条；确认无图的条目记 image='-1'（前端按无图处理），不再重试。"""
        rows = db.all(
            "SELECT id, guid, url FROM items WHERE source_id = ? AND image = ''"
            " ORDER BY id DESC LIMIT 10",
            (source_id,),
        )
        if not rows:
            return
        log.info("reddit enrich: %d candidate item(s)", len(rows))
        try:
            token = await get_token(self.app_settings)
        except Exception:
            log.warning("reddit enrich skipped: no reddit credentials", exc_info=True)
            return
        async with httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        ) as client:
            for row in rows:
                guid = row["guid"] or ""
                post_id = guid[3:] if guid.startswith("t3_") else guid
                if not post_id and "comments/" in (row["url"] or ""):
                    post_id = row["url"].split("comments/", 1)[1].split("/")[0].split("?")[0]
                if not post_id:
                    continue
                try:
                    resp = await client.get(
                        f"https://oauth.reddit.com/comments/{post_id}?limit=1&raw_json=1"
                    )
                    resp.raise_for_status()
                    # 该端点返回 [帖子列表, 评论列表] 数组
                    payload = resp.json()
                    listing = payload[0] if isinstance(payload, list) else payload
                    children = listing.get("data", {}).get("children", [])
                    d = children[0].get("data", {}) if children else {}
                except Exception:
                    log.warning(
                        "reddit enrich fetch failed (item=%s post=%s)",
                        row["id"], post_id, exc_info=True,
                    )
                    break  # 网络问题：停下，留给下一轮
                video_duration = int(
                    ((d.get("media") or {}).get("reddit_video") or {}).get("duration") or 0
                )
                image = _preview_url(d) or "-1"
                db.conn.execute(
                    "UPDATE items SET image = ?, duration = ? WHERE id = ?",
                    (image, video_duration, row["id"]),
                )
                db.conn.commit()

    async def archive_requests(self, item: dict) -> list[ArchiveRequest]:
        token = await get_token(self.app_settings)
        guid = item.get("guid") or ""
        url = item.get("url") or ""
        post_id = guid[3:] if guid.startswith("t3_") else guid
        if not post_id and "comments/" in url:
            post_id = url.split("comments/", 1)[1].split("/")[0].split("?")[0]
        fallback = ArchiveRequest(
            url=url or f"https://www.reddit.com/comments/{post_id}",
            kind="html",
        )
        if not post_id:
            return [fallback]
        return [
            ArchiveRequest(
                url=f"https://oauth.reddit.com/comments/{post_id}?limit=20&sort=hot&raw_json=1",
                kind="json",
                filename=f"{post_id}.json",
                headers={"Authorization": f"Bearer {token}"},
            ),
            fallback,
        ]

    def viewer(self, item: dict, assets: list[dict]) -> dict:
        by_kind: dict[str, dict] = {}
        for asset in reversed(assets):
            by_kind.setdefault(asset["kind"], asset)
        for kind in ("json", "html", "pdf", "image", "video", "file"):
            if kind in by_kind:
                return {"kind": kind, "asset": by_kind[kind]}
        return {"kind": "none"}
