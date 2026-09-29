"""P0-1 回归：全新库（尚未写入向量）上删除源，不应因 item_vectors 缺表而 500。"""

from app.core.api import CoreAPI
from app.core.db import Database


def test_delete_source_on_fresh_db(tmp_path):
    database = Database(tmp_path / "fresh.db")
    database.init()
    class _WS:
        def __init__(self, db):
            self.db = db

    class _Mgr:
        def __init__(self, db):
            self._ws = _WS(db)

        def current(self):
            return self._ws

    api = CoreAPI.__new__(CoreAPI)
    api.manager = _Mgr(database)
    api.settings = None
    api.embedder = None
    api.app_settings = None
    database.conn.execute(
        "INSERT INTO sources (name, url, type) VALUES ('t', 'http://x', 'rss')"
    )
    sid = database.conn.execute("SELECT LAST_INSERT_ROWID()").fetchone()[0]
    database.conn.execute(
        "INSERT INTO items (source_id, title, guid_hash) VALUES (?, 'a', 'h1')",
        (sid,),
    )
    database.conn.commit()
    api.delete_source(sid)
    assert database.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    assert database.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
