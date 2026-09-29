from __future__ import annotations

import hashlib
import logging
import re
from html.parser import HTMLParser
from pathlib import Path

import httpx

from app.core.services.ai import generate_markdown
from app.core.sources import get_source
from app.core.sources.base import USER_AGENT

log = logging.getLogger("deepaggregator.fulltext")

MIME_EXT = {
    "application/pdf": "pdf",
    "text/html": "html",
    "text/plain": "txt",
    "text/markdown": "md",
    "application/json": "json",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
}
KIND_EXT = {
    "pdf": "pdf",
    "html": "html",
    "md": "md",
    "json": "json",
    "image": "img",
    "video": "mp4",
    "file": "bin",
}
PAYLOAD_CAPTURE_LIMIT = 4 * 1024 * 1024
_ILLEGAL = re.compile(r'[<>:"|\\?*\x00-\x1f]')


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template"}
    _BLOCK = {
        "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "br", "blockquote", "pre", "table", "ul", "ol",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"[ \t]+", " ", "".join(parser.parts))


def sanitize_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _ILLEGAL.sub("", name).strip(". ")[:150]
    return name or "archive"


def derive_filename(url: str, kind: str) -> str:
    segment = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    name = sanitize_filename(segment)
    if not name:
        name = "archive"
    if "." not in name:
        name += "." + KIND_EXT.get(kind, "bin")
    return name


def _classify(mime: str, wanted_kind: str) -> tuple[str, str]:
    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    if wanted_kind != "auto":
        kind = wanted_kind
    elif mime == "application/pdf":
        kind = "pdf"
    elif mime.startswith("image/"):
        kind = "image"
    elif mime == "text/html":
        kind = "html"
    elif mime.startswith("video/"):
        kind = "video"
    else:
        kind = "file"
    ext = MIME_EXT.get(mime) or KIND_EXT.get(kind, "bin")
    return kind, ext


def unique_asset_name(conn, source_type: str, filename: str, item_id: int) -> tuple[str, str]:
    """为资产定名：与其它条目的既有资产撞名时追加 -2 后缀。返回 (rel_path, filename)。"""
    rel_path = f"{source_type}/{filename}"
    occupied = conn.execute(
        "SELECT item_id FROM assets WHERE path = ? AND item_id != ?", (rel_path, item_id)
    ).fetchone()
    if occupied is None:
        return rel_path, filename
    stem, dot, ext = filename.rpartition(".")
    n = 2
    while True:
        cand = f"{stem}-{n}.{ext}" if dot else f"{filename}-{n}"
        rel_path = f"{source_type}/{cand}"
        if not conn.execute("SELECT 1 FROM assets WHERE path = ?", (rel_path,)).fetchone():
            return rel_path, cand
        n += 1


class ArchiveError(RuntimeError):
    """归档无法完成（条目缺失、全部请求失败等），由队列转为 failed 状态。"""


async def execute_archive(core, db, item_id: int, on_event=None) -> dict:
    """执行单条归档；on_event 收到 {request_index, request_count, url} 进度。

    成功返回 {kind, name, size, mime}；失败抛 ArchiveError（fulltext_state 由调用方处置，
    本函数只负责落 pending/failed 两个中间态）。
    """
    cfg = core.settings
    conn = db.conn
    item = conn.execute(
        "SELECT id, url, source_id, guid FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None or not item["url"]:
        raise ArchiveError("条目不存在或没有可存档的链接")
    source = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (item["source_id"],)
    ).fetchone()
    conn.execute("UPDATE items SET fulltext_state = 'pending' WHERE id = ?", (item_id,))
    conn.commit()

    module = get_source(source, core.app_settings)
    requests_list = await module.archive_requests(dict(item))
    request_count = max(1, len(requests_list))
    old_assets = [
        dict(r)
        for r in conn.execute(
            "SELECT id, path FROM assets WHERE item_id = ?", (item_id,)
        ).fetchall()
    ]
    assets_dir = Path(db.path).parent / "assets" / module.type
    assets_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = assets_dir / f".tmp-{item_id}"
    ai_cfg = core.app_settings.data.get("ai") or {}
    markdown_mode = bool(source["archive_markdown"])

    new_rel_path: str | None = None
    new_row_id: int | None = None

    def save_asset(data: bytes, asset_kind: str, asset_mime: str, asset_name: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        rel_path = f"{module.type}/{asset_name}"
        dest = assets_dir / asset_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        cur = conn.execute(
            "INSERT INTO assets(item_id, kind, mime, size, sha256, path, name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, asset_kind, asset_mime, len(data), sha, rel_path, asset_name),
        )
        return rel_path, cur.lastrowid

    saved = False
    result: dict = {}
    try:
        for req_index, req in enumerate(requests_list, start=1):
            if on_event is not None:
                on_event(
                    {
                        "request_index": req_index,
                        "request_count": request_count,
                        "url": req.url,
                    }
                )
            filename = (
                sanitize_filename(req.filename)
                if req.filename
                else derive_filename(req.url, req.kind)
            )
            rel_path, filename = unique_asset_name(conn, module.type, filename, item_id)
            try:
                payload: bytearray | None = bytearray() if req.kind in ("html", "auto") else None
                async with httpx.AsyncClient(
                    timeout=cfg.archive_timeout,
                    follow_redirects=True,
                    headers={"User-Agent": USER_AGENT, **(req.headers or {})},
                ) as client:
                    async with client.stream("GET", req.url) as resp:
                        resp.raise_for_status()
                        mime = (
                            resp.headers.get("content-type", "application/octet-stream")
                            .split(";")[0]
                            .strip()
                            .lower()
                            or "application/octet-stream"
                        )
                        digest = hashlib.sha256()
                        size = 0
                        with open(tmp_path, "wb") as fh:
                            async for chunk in resp.aiter_bytes(65536):
                                size += len(chunk)
                                if size > cfg.archive_max_bytes:
                                    raise ValueError("asset exceeds size limit")
                                digest.update(chunk)
                                fh.write(chunk)
                                if payload is not None:
                                    payload.extend(chunk)
                                    if len(payload) > PAYLOAD_CAPTURE_LIMIT:
                                        payload = None
                kind, ext = _classify(mime, req.kind)
                if req.kind != "auto" and kind != req.kind:
                    raise ValueError(f"expected {req.kind}, server returned {mime}")

                if markdown_mode and kind == "html" and payload:
                    try:
                        md_text = await generate_markdown(
                            ai_cfg,
                            html_to_text(bytes(payload).decode("utf-8", "replace")),
                        )
                        md_name = (filename.rpartition(".")[0] or filename) + ".md"
                        new_rel_path, new_row_id = save_asset(
                            md_text.encode("utf-8"), "md", "text/markdown", md_name
                        )
                        result = {
                            "kind": "md",
                            "name": md_name,
                            "size": len(md_text.encode("utf-8")),
                            "mime": "text/markdown",
                        }
                        saved = True
                        break
                    except Exception:
                        payload = None

                sha = digest.hexdigest()
                dest = assets_dir / filename
                tmp_path.replace(dest)
                cur = conn.execute(
                    "INSERT INTO assets(item_id, kind, mime, size, sha256, path, name)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, kind, mime, size, sha, rel_path, filename),
                )
                new_rel_path = rel_path
                new_row_id = cur.lastrowid
                result = {"kind": kind, "name": filename, "size": size, "mime": mime}
                saved = True
                break
            except Exception as exc:
                tmp_path.unlink(missing_ok=True)
                log.warning(
                    "archive request failed (item=%s kind=%s url=%s): %s",
                    item_id, req.kind, req.url, exc,
                )
                continue
        if not saved:
            raise ArchiveError("所有存档请求均失败")
        if new_row_id is not None:
            for stale in old_assets:
                if stale["id"] == new_row_id:
                    continue
                if stale["path"] != new_rel_path:
                    try:
                        (Path(db.path).parent / "assets" / stale["path"]).unlink(missing_ok=True)
                    except OSError:
                        pass
                conn.execute("DELETE FROM assets WHERE id = ?", (stale["id"],))
        conn.execute("UPDATE items SET fulltext_state = 'done' WHERE id = ?", (item_id,))
        conn.commit()
        return result
    except Exception:
        tmp_path.unlink(missing_ok=True)
        conn.execute("UPDATE items SET fulltext_state = 'failed' WHERE id = ?", (item_id,))
        conn.commit()
        raise
