import hashlib

from app.core.services import source_health
from app.core.sources import get_source
from app.core.sources.base import RawItem


def _guid_hash(item: RawItem) -> str:
    return hashlib.sha256((item.guid or item.url).encode("utf-8")).hexdigest()


def upsert_items(db, source_id: int, items: list[RawItem]) -> int:
    conn = db.conn
    inserted = 0
    for item in items:
        if not (item.guid or item.url):
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO items(source_id, guid_hash, guid, title, summary, url, author, published_at, image, duration)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                _guid_hash(item),
                item.guid,
                item.title,
                item.summary,
                item.url,
                item.author,
                item.published_at.isoformat() if item.published_at else None,
                item.image,
                item.duration,
            ),
        )
        if cursor.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted


async def fetch_source(db, source, app_settings=None) -> dict:
    module = get_source(source, app_settings)
    try:
        raw_items = await module.fetch()
    except Exception as exc:
        if source_health.is_credential_error(module.type, str(exc)):
            source_health.report(module.type, source["name"], str(exc))
        raise
    inserted = upsert_items(db, source["id"], raw_items)
    conn = db.conn
    conn.execute(
        "UPDATE sources SET last_fetched_at = datetime('now') WHERE id = ?",
        (source["id"],),
    )
    conn.commit()
    source_health.clear(module.type)
    await module.enrich_items(db, source["id"])
    return {"fetched": len(raw_items), "inserted": inserted}
