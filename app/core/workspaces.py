import re
import shutil
import sqlite3
import threading
from pathlib import Path

from app.core.db import Database

POINTER_FILE = ".current"
RESERVED_DIRS = {"assets", "ai_media"}  # 数据卷保留目录，不是工作区
NAME_RE = re.compile(r"^[^/\\<>:\"|?*\x00-\x1f]{1,64}$")


def _valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and not name.startswith(".")


class Workspace:
    def __init__(self, name: str, path: Path):
        self.name = name
        self.path = Path(path)
        self._db: Database | None = None

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.path / "aggregator.db")
            self._db.init()
        return self._db


class WorkspaceManager:
    def __init__(self, root: Path, initial: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, Workspace] = {}
        self._active_name = ""
        self._migrate_legacy()
        self._bootstrap(initial)

    def _scan_unlocked(self) -> list[str]:
        # 工作区 = 含 aggregator.db 的目录，或空目录（新建后尚未落库）。
        # 其余（assets/、ai_media/ 等保留目录，或只有杂物的目录）一律不算。
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir()
            and not p.name.startswith(".")
            and _valid_name(p.name)
            and p.name not in RESERVED_DIRS
            and ((p / "aggregator.db").exists() or not any(p.iterdir()))
        )

    def scan(self) -> list[str]:
        with self._lock:
            return self._scan_unlocked()

    def _migrate_legacy(self) -> None:
        legacy_db = self.root / "aggregator.db"
        if not legacy_db.exists():
            return
        if (self.root / "default").exists():
            return  # 已有新布局，根上的旧库/旧资产按遗留物处理（不迁移不清理）
        ws = self._ensure_dir("default")
        legacy_assets = self.root / "assets"
        if legacy_assets.exists():
            shutil.move(str(legacy_assets), str(ws.path / "assets"))
        for suffix in ("", "-wal", "-shm"):
            src = self.root / f"aggregator.db{suffix}"
            if src.exists():
                shutil.move(str(src), str(ws.path / f"aggregator.db{suffix}"))

    def _ensure_dir(self, name: str) -> Workspace:
        ws = Workspace(name, self.root / name)
        ws.path.mkdir(parents=True, exist_ok=True)
        self._cache[name] = ws
        return ws

    def _bootstrap(self, initial: str) -> None:
        with self._lock:
            names = self._scan_unlocked()
            if initial:
                if not _valid_name(initial):
                    raise ValueError(f"invalid workspace name: {initial!r}")
                if initial not in names:
                    self._ensure_dir(initial)
                target = initial
            else:
                pointer = self._read_pointer()
                if pointer and pointer in self._scan_unlocked():
                    target = pointer
                elif names:
                    target = names[0]
                else:
                    self._ensure_dir("default")
                    target = "default"
            ws = self._cache.get(target) or self._ensure_dir(target)
            self._active_name = target
        self._write_pointer(target)
        _ = ws.db

    def _read_pointer(self) -> str:
        pointer = self.root / POINTER_FILE
        if pointer.exists():
            try:
                name = pointer.read_text(encoding="utf-8").strip()
                if name and _valid_name(name):
                    return name
            except OSError:
                pass
        return ""

    def _write_pointer(self, name: str) -> None:
        tmp = self.root / (POINTER_FILE + ".tmp")
        tmp.write_text(name, encoding="utf-8")
        tmp.replace(self.root / POINTER_FILE)

    def create(self, name: str) -> Workspace:
        if not _valid_name(name):
            raise ValueError(f"invalid workspace name: {name!r}")
        with self._lock:
            if name in self._scan_unlocked():
                raise FileExistsError(name)
            ws = self._ensure_dir(name)
        _ = ws.db
        return ws

    def activate(self, name: str) -> Workspace:
        with self._lock:
            if name not in self._scan_unlocked():
                raise KeyError(name)
            ws = self._cache.get(name) or self._ensure_dir(name)
            self._active_name = name
        self._write_pointer(name)
        return ws

    def current(self) -> Workspace:
        with self._lock:
            return self._cache[self._active_name]

    def get(self, name: str) -> Workspace:
        with self._lock:
            ws = self._cache.get(name)
            if ws is None:
                ws = Workspace(name, self.root / name)
                self._cache[name] = ws
            return ws

    @property
    def active_name(self) -> str:
        with self._lock:
            return self._active_name

    def summaries(self) -> list[dict]:
        active = self.active_name
        out = []
        for name in self.scan():
            db_path = self.root / name / "aggregator.db"
            items = unread = sources = 0
            if db_path.exists():
                conn = sqlite3.connect(db_path, timeout=5)
                conn.row_factory = sqlite3.Row
                try:
                    items = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
                    unread = conn.execute(
                        "SELECT COUNT(*) AS c FROM items WHERE is_read = 0"
                    ).fetchone()["c"]
                    sources = conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"]
                except sqlite3.OperationalError:
                    pass
                finally:
                    conn.close()
            out.append(
                {
                    "name": name,
                    "items": items,
                    "unread": unread,
                    "sources": sources,
                    "active": name == active,
                }
            )
        return out
