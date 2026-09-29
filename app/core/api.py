import hashlib
import json
import mimetypes
import shutil
from pathlib import Path

from app.core.sources import get_source
from app.core.services import fulltext
from app.core.services.embeddings import build_embedder


class CoreAPI:
    def __init__(self, manager, settings, embedder, app_settings):
        self.manager = manager
        self.settings = settings
        self.embedder = embedder
        self.app_settings = app_settings
        self.jobs = None
        self.queue = None

    @property
    def db(self):
        return self.manager.current().db

    def apply_settings(self, patch: dict) -> dict:
        view = self.app_settings.update(patch)
        self.embedder = build_embedder(self.app_settings.merged())
        return view

    def settings_view(self) -> dict:
        return self.app_settings.view()

    def list_sources(self) -> list[dict]:
        rows = self.db.all(
            "SELECT s.*, COUNT(i.id) AS item_count,"
            " COALESCE(SUM(CASE WHEN i.is_read = 0 THEN 1 ELSE 0 END), 0) AS unread_count"
            " FROM sources s LEFT JOIN items i ON i.source_id = s.id"
            " GROUP BY s.id ORDER BY s.type, s.sort_order, s.id"
        )
        return [dict(r) for r in rows]

    def get_source_prefs(self) -> dict:
        row = self.db.one("SELECT value FROM ui_prefs WHERE key = 'source_groups'")
        try:
            return {"group_order": json.loads(row["value"]) if row else []}
        except (ValueError, TypeError):
            return {"group_order": []}

    def set_source_prefs(self, group_order: list, source_order: list) -> None:
        """持久化源列表 UI：分组顺序 + 组内源顺序。"""
        conn = self.db.conn
        for pos, sid in enumerate(source_order):
            conn.execute(
                "UPDATE sources SET sort_order = ? WHERE id = ?", (pos, int(sid))
            )
        conn.execute(
            "INSERT INTO ui_prefs(key, value) VALUES ('source_groups', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(group_order),),
        )
        conn.commit()

    def create_source(
        self,
        type_,
        name,
        url,
        fetch_interval_min,
        config,
        color="",
        archive_enabled=True,
        archive_markdown=False,
        allow_upload=False,
    ) -> dict:
        cur = self.db.conn.execute(
            "INSERT INTO sources(type, name, url, config_json, fetch_interval_min, color,"
            " archive_enabled, archive_markdown, allow_upload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                type_,
                name,
                url,
                json.dumps(config or {}),
                fetch_interval_min,
                color,
                int(archive_enabled),
                int(archive_markdown),
                int(allow_upload),
            ),
        )
        self.db.conn.commit()
        return dict(self.db.one("SELECT * FROM sources WHERE id = ?", (cur.lastrowid,)))

    def update_source(self, source_id: int, fields: dict) -> dict | None:
        allowed = {
            "name",
            "url",
            "fetch_interval_min",
            "config",
            "enabled",
            "color",
            "archive_enabled",
            "archive_markdown",
            "allow_upload",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "config":
                value = json.dumps(value or {})
            if key in (
                "enabled",
                "archive_enabled",
                "archive_markdown",
                "allow_upload",
            ) and isinstance(value, bool):
                value = int(value)
            sets.append(f"{key} = ?")
            params.append(value)
        if sets:
            params.append(source_id)
            self.db.conn.execute(
                f"UPDATE sources SET {', '.join(sets)} WHERE id = ?", params
            )
            self.db.conn.commit()
        row = self.db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
        return dict(row) if row else None

    def delete_source(self, source_id: int) -> None:
        # item_vectors 是 vec0 虚表，首次写向量时才惰性创建——新库直接删会 OperationalError
        if self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'item_vectors'"
        ).fetchone():
            self.db.conn.execute(
                "DELETE FROM item_vectors WHERE item_id IN"
                " (SELECT id FROM items WHERE source_id = ?)",
                (source_id,),
            )
        self.db.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self.db.conn.commit()

    async def refresh_source(self, source_id: int) -> dict | None:
        from app.core.services import ingest

        source = self.db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
        if source is None:
            return None
        return await ingest.fetch_source(self.db, source, self.app_settings)

    def get_item(self, item_id: int) -> dict | None:
        row = self.db.one(
            "SELECT i.*, s.name AS source_name, s.color AS source_color,"
            " s.archive_enabled AS archive_enabled, s.allow_upload AS allow_upload"
            " FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id = ?",
            (item_id,),
        )
        if row is None:
            return None
        item = dict(row)
        item["assets"] = [
            dict(r)
            for r in self.db.all(
                "SELECT id, kind, mime, size, sha256, path, name, created_at"
                " FROM assets WHERE item_id = ?",
                (item_id,),
            )
        ]
        source_row = self.db.one(
            "SELECT * FROM sources WHERE id = ?", (item["source_id"],)
        )
        if source_row is not None:
            item["viewer"] = get_source(source_row, self.app_settings).viewer(
                item, item["assets"]
            )
        else:
            item["viewer"] = {"kind": "none"}
        return item

    def set_item_flags(self, item_id: int, is_read=None, is_starred=None, is_bookmarked=None) -> dict | None:
        sets, params = [], []
        if is_read is not None:
            sets.append("is_read = ?")
            params.append(int(is_read))
        if is_starred is not None:
            sets.append("is_starred = ?")
            params.append(int(is_starred))
        if is_bookmarked is not None:
            sets.append("is_bookmarked = ?")
            params.append(int(is_bookmarked))
        if sets:
            params.append(item_id)
            self.db.conn.execute(
                f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params
            )
            self.db.conn.commit()
        return self.get_item(item_id)

    def mark_read_bulk(self, source_id=None, type_=None) -> int:
        """批量标记已读：按单源或按源类型。返回标记条数。"""
        if source_id:
            cur = self.db.conn.execute(
                "UPDATE items SET is_read = 1 WHERE source_id = ? AND is_read = 0",
                (source_id,),
            )
        elif type_:
            cur = self.db.conn.execute(
                "UPDATE items SET is_read = 1 WHERE is_read = 0 AND source_id IN"
                " (SELECT id FROM sources WHERE type = ?)",
                (type_,),
            )
        else:
            return 0
        self.db.conn.commit()
        return cur.rowcount

    async def search_items(self, q, mode, source_id, unread, starred, limit, offset, type_="", bookmarked=False,
                           source_ids="", types="") -> dict:
        from app.core.services import search

        return await search.search(
            self.db, self.embedder, q, mode, source_id, unread, starred, limit, offset, type_, bookmarked,
            source_ids, types
        )

    def request_archive(self, item_id: int) -> dict:
        assert self.queue is not None
        item = self.db.one(
            "SELECT id, title, source_id, fulltext_state FROM items WHERE id = ?",
            (item_id,),
        )
        if item is None:
            return {"state": "none", "queued": False}
        if item["fulltext_state"] == "pending" or self.queue.is_enqueued(
            str(self.db.path), item_id
        ):
            return {"state": "pending", "queued": False}
        self.queue.enqueue(self.db, item_id, item["source_id"], item["title"])
        return {"state": "pending", "queued": True}

    def queue_snapshot(self) -> dict:
        assert self.queue is not None
        tasks = self.queue.snapshot(self.db.path)
        names = {r["id"]: r["name"] for r in self.db.all("SELECT id, name FROM sources")}
        for task in tasks:
            task["source_name"] = names.get(task["source_id"], "已删除源")
        return {"tasks": tasks}

    def delete_archive(self, item_id: int) -> dict:
        assert self.queue is not None
        item = self.db.one(
            "SELECT id, source_id FROM items WHERE id = ?", (item_id,)
        )
        if item is None:
            raise KeyError(item_id)
        if self.queue.is_enqueued(str(self.db.path), item_id):
            raise RuntimeError("该条目的存档任务正在进行中，请稍后再试")
        rows = self.db.all("SELECT path FROM assets WHERE item_id = ?", (item_id,))
        assets_dir = (self.manager.current().path / "assets").resolve()
        for row in rows:
            path = (assets_dir / row["path"]).resolve()
            if path.is_relative_to(assets_dir):
                path.unlink(missing_ok=True)
        self.db.conn.execute("DELETE FROM assets WHERE item_id = ?", (item_id,))
        self.db.conn.execute(
            "UPDATE items SET fulltext_state = 'none' WHERE id = ?", (item_id,)
        )
        self.db.conn.commit()
        self.queue.purge_history(self.db.path, item_id)
        self.queue.publish_deleted(
            self.manager.active_name, item_id, item["source_id"]
        )
        return self.get_item(item_id)

    def upload_archive(self, item_id: int, filename: str, content_type: str, tmp_path) -> dict:
        """用户上传本地文件作为该条目的存档（覆盖语义）。仅限开启 allow_upload 的源。

        tmp_path 为路由层已落盘的临时文件；本方法负责定名、归类、替换旧资产并广播。
        抛 KeyError(404) / PermissionError(403) / RuntimeError(409) / ValueError(413 超限)。
        """
        assert self.queue is not None
        row = self.db.one(
            "SELECT i.source_id, s.type AS source_type, s.allow_upload"
            " FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id = ?",
            (item_id,),
        )
        if row is None:
            raise KeyError(item_id)
        if not row["allow_upload"]:
            raise PermissionError("该源未开启「允许上传存档」")
        if self.queue.is_enqueued(str(self.db.path), item_id):
            raise RuntimeError("该条目的存档任务正在进行中，请稍后再试")
        safe_name = fulltext.sanitize_filename(filename or "")
        mime = (content_type or "").split(";")[0].strip().lower()
        if mime in ("", "application/octet-stream"):
            mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        kind, _ = fulltext._classify(mime, "auto")
        tmp = Path(tmp_path)
        if tmp.stat().st_size > self.settings.archive_max_bytes:
            limit_mb = self.settings.archive_max_bytes // (1024 * 1024)
            raise ValueError(f"文件超过大小上限（{limit_mb} MB）")

        assets_dir = self.manager.current().path / "assets" / row["source_type"]
        assets_dir.mkdir(parents=True, exist_ok=True)
        old_assets = [
            dict(r)
            for r in self.db.conn.execute(
                "SELECT id, path FROM assets WHERE item_id = ?", (item_id,)
            ).fetchall()
        ]
        rel_path, final_name = fulltext.unique_asset_name(
            self.db.conn, row["source_type"], safe_name, item_id
        )
        dest = assets_dir / final_name
        shutil.move(str(tmp), dest)
        digest = hashlib.sha256()
        with open(dest, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        file_size = dest.stat().st_size
        self.db.conn.execute("DELETE FROM assets WHERE item_id = ?", (item_id,))
        for stale in old_assets:
            stale_path = (assets_dir.parent / stale["path"]).resolve()
            if stale_path != dest.resolve() and stale_path.is_relative_to(
                assets_dir.parent.resolve()
            ):
                stale_path.unlink(missing_ok=True)
        self.db.conn.execute(
            "INSERT INTO assets(item_id, kind, mime, size, sha256, path, name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, kind, mime, file_size, digest.hexdigest(), rel_path, final_name),
        )
        self.db.conn.execute(
            "UPDATE items SET fulltext_state = 'done' WHERE id = ?", (item_id,)
        )
        self.db.conn.commit()
        self.queue.publish_uploaded(
            self.manager.active_name,
            item_id,
            row["source_id"],
            final_name,
            file_size,
            kind,
        )
        return self.get_item(item_id)

    def asset_file(self, asset_id: int):
        row = self.db.one("SELECT * FROM assets WHERE id = ?", (asset_id,))
        if row is None:
            return None
        assets_dir = (self.manager.current().path / "assets").resolve()
        path = (assets_dir / row["path"]).resolve()
        if not path.is_relative_to(assets_dir) or not path.is_file():
            return None
        return path, row["mime"], row["name"] or path.name

    def stats(self) -> dict:
        conn = self.db.conn
        assets = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS bytes FROM assets"
        ).fetchone()
        return {
            "workspace": self.manager.active_name,
            "items": conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"],
            "unread": conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE is_read = 0"
            ).fetchone()["c"],
            "starred": conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE is_starred = 1"
            ).fetchone()["c"],
            "sources": conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"],
            "vectors": conn.execute(
                "SELECT COUNT(*) AS c FROM vector_meta WHERE model = ?",
                (self.embedder.name,),
            ).fetchone()["c"],
            "assets": assets["c"],
            "asset_bytes": assets["bytes"],
            "db_bytes": self.db.path.stat().st_size if self.db.path.exists() else 0,
            "embedder": self.embedder.name,
        }
