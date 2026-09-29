import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK(type IN ('rss', 'arxiv', 'reddit', 'youtube', 'bilibili')),
  name TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  config_json TEXT NOT NULL DEFAULT '{}',
  fetch_interval_min INTEGER NOT NULL DEFAULT 60,
  enabled INTEGER NOT NULL DEFAULT 1,
  color TEXT NOT NULL DEFAULT '',
  archive_enabled INTEGER NOT NULL DEFAULT 1,
  archive_markdown INTEGER NOT NULL DEFAULT 0,
  allow_upload INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  last_fetched_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ui_prefs(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '新对话',
  pinned INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  content TEXT NOT NULL,
  contexts_json TEXT NOT NULL DEFAULT '[]',
  media_json TEXT NOT NULL DEFAULT '[]',
  rating INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS chat_session_items(
  session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL,
  PRIMARY KEY (session_id, item_id)
);

CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  guid_hash TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  duration INTEGER NOT NULL DEFAULT 0,
  published_at TEXT,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
  is_read INTEGER NOT NULL DEFAULT 0,
  is_starred INTEGER NOT NULL DEFAULT 0,
  is_bookmarked INTEGER NOT NULL DEFAULT 0,
  fulltext_state TEXT NOT NULL DEFAULT 'none'
    CHECK(fulltext_state IN ('none','pending','done','failed'))
);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id, id DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, summary, content='items', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary)
  VALUES ('delete', old.id, old.title, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE OF title, summary ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary)
  VALUES ('delete', old.id, old.title, old.summary);
  INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;

CREATE TABLE IF NOT EXISTS vector_meta(
  item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vector_meta_model ON vector_meta(model);

CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.conn
        conn.executescript(SCHEMA)
        asset_cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets)")]
        if asset_cols and "name" not in asset_cols:
            conn.execute("ALTER TABLE assets ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        source_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sources)")]
        item_cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
        if item_cols and "guid" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN guid TEXT NOT NULL DEFAULT ''")
        if item_cols and "image" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN image TEXT NOT NULL DEFAULT ''")
        if item_cols and "duration" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN duration INTEGER NOT NULL DEFAULT 0")
        if item_cols and "is_bookmarked" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN is_bookmarked INTEGER NOT NULL DEFAULT 0")
        chat_cols = [r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)")]
        if chat_cols and "media_json" not in chat_cols:
            conn.execute(
                "ALTER TABLE chat_messages ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'"
            )
        if chat_cols and "rating" not in chat_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN rating INTEGER")
        if chat_cols and "reasoning_json" not in chat_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN reasoning_json TEXT")
        sess_cols = [r["name"] for r in conn.execute("PRAGMA table_info(chat_sessions)")]
        if sess_cols and "pinned" not in sess_cols:
            conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )
        if sess_cols and "archived" not in sess_cols:
            conn.execute(
                "ALTER TABLE chat_sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
            )
        for col, ddl in (
            ("color", "TEXT NOT NULL DEFAULT ''"),
            ("archive_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("archive_markdown", "INTEGER NOT NULL DEFAULT 0"),
            ("allow_upload", "INTEGER NOT NULL DEFAULT 0"),
            ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if source_cols and col not in source_cols:
                conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {ddl}")
        src_rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchall()
        src_row = src_rows[0] if src_rows else None
        if src_row and src_row["sql"] and "'bilibili'" not in src_row["sql"]:
            conn.executescript(
                """
                PRAGMA foreign_keys=OFF;
                BEGIN;
                DROP TABLE IF EXISTS sources_new;
                CREATE TABLE sources_new (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  type TEXT NOT NULL CHECK(type IN ('rss', 'arxiv', 'reddit', 'youtube', 'bilibili')),
                  name TEXT NOT NULL,
                  url TEXT NOT NULL DEFAULT '',
                  config_json TEXT NOT NULL DEFAULT '{}',
                  fetch_interval_min INTEGER NOT NULL DEFAULT 60,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  color TEXT NOT NULL DEFAULT '',
                  archive_enabled INTEGER NOT NULL DEFAULT 1,
                  archive_markdown INTEGER NOT NULL DEFAULT 0,
                  allow_upload INTEGER NOT NULL DEFAULT 0,
                  sort_order INTEGER NOT NULL DEFAULT 0,
                  last_fetched_at TEXT,
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO sources_new(id, type, name, url, config_json,
                  fetch_interval_min, enabled, color, archive_enabled,
                  archive_markdown, allow_upload, sort_order, last_fetched_at, created_at)
                  SELECT id, type, name, url, config_json, fetch_interval_min,
                  enabled, color, archive_enabled, archive_markdown,
                  COALESCE(allow_upload, 0), COALESCE(sort_order, 0), last_fetched_at, created_at FROM sources;
                DROP TABLE sources;
                ALTER TABLE sources_new RENAME TO sources;
                COMMIT;
                PRAGMA foreign_keys=ON;
                """
            )
        conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._load_vec_extension(conn)
            self._local.conn = conn
        return conn

    @staticmethod
    def _load_vec_extension(conn: sqlite3.Connection) -> None:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.conn.execute(sql, params).fetchall()
        return rows[0] if rows else None

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()
