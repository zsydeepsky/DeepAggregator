from __future__ import annotations

from app.core.services.embeddings import knn_search

ITEM_FIELDS = (
    "i.id, i.source_id, s.name AS source_name, s.color AS source_color, i.title,"
    " i.summary, i.url, i.author, i.image, i.duration, i.published_at, i.fetched_at,"
    " i.is_read, i.is_starred, i.is_bookmarked, i.fulltext_state"
)


def _filter_clause(source_id, unread, starred, type_="", bookmarked=False,
                   source_ids="", types=""):
    conds, params = [], []
    if source_id:
        conds.append("i.source_id = ?")
        params.append(source_id)
    if type_:
        conds.append("s.type = ?")
        params.append(type_)
    id_list = [int(x) for x in source_ids.split(",") if x.strip().isdigit()] if source_ids else []
    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else []
    if id_list and type_list:
        # 并集：所选的具体源 + 所选的整类型
        ph_ids = ",".join("?" * len(id_list))
        ph_types = ",".join("?" * len(type_list))
        conds.append(f"(i.source_id IN ({ph_ids}) OR s.type IN ({ph_types}))")
        params.extend(id_list)
        params.extend(type_list)
    elif id_list:
        conds.append(f"i.source_id IN ({','.join('?' * len(id_list))})")
        params.extend(id_list)
    elif type_list:
        conds.append(f"s.type IN ({','.join('?' * len(type_list))})")
        params.extend(type_list)
    if unread:
        conds.append("i.is_read = 0")
    if starred:
        conds.append("i.is_starred = 1")
    if bookmarked:
        conds.append("i.is_bookmarked = 1")
    return conds, params


def _load_items(db, model: str, ids: list[int], conds=None, cond_params=None) -> list[dict]:
    if not ids:
        return []
    conds = conds or []
    cond_params = cond_params or []
    placeholders = ",".join("?" * len(ids))
    where = f" AND {' AND '.join(conds)}" if conds else ""
    rows = db.all(
        f"SELECT {ITEM_FIELDS}, (v.item_id IS NOT NULL) AS has_vector"
        " FROM items i JOIN sources s ON s.id = i.source_id"
        " LEFT JOIN vector_meta v ON v.item_id = i.id AND v.model = ?"
        f" WHERE i.id IN ({placeholders}){where}",
        (model, *ids, *cond_params),
    )
    order = {item_id: pos for pos, item_id in enumerate(ids)}
    return sorted((dict(r) for r in rows), key=lambda d: order[d["id"]])


def _attach_scores(items: list[dict], scores: dict[int, float]) -> None:
    for item in items:
        s = scores.get(item["id"])
        if s is not None:
            item["score"] = round(min(1.0, max(0.0, float(s))), 4)


def _recent(db, model, source_id, unread, starred, limit, offset, type_="", bookmarked=False,
            source_ids="", types="") -> dict:
    conds, params = _filter_clause(source_id, unread, starred, type_, bookmarked, source_ids, types)
    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    rows = db.all(
        f"SELECT {ITEM_FIELDS}, (v.item_id IS NOT NULL) AS has_vector"
        " FROM items i JOIN sources s ON s.id = i.source_id"
        " LEFT JOIN vector_meta v ON v.item_id = i.id AND v.model = ?"
        f"{where}"
        " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC LIMIT ? OFFSET ?",
        (model, *params, limit + 1, offset),
    )
    items = [dict(r) for r in rows]
    has_more = len(items) > limit
    return {"items": items[:limit], "has_more": has_more, "mode": "recent"}


def keyword_ids(db, q: str, cap: int = 200) -> list[int]:
    q = q.strip()
    if not q:
        return []
    conn = db.conn
    if len(q) >= 3:
        try:
            rows = conn.execute(
                "SELECT rowid FROM items_fts WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
                ('"' + q.replace('"', '""') + '"', cap),
            ).fetchall()
            return [r["rowid"] for r in rows]
        except Exception:
            pass
    rows = conn.execute(
        "SELECT id FROM items WHERE title LIKE ? OR summary LIKE ? ORDER BY id DESC LIMIT ?",
        (f"%{q}%", f"%{q}%", cap),
    ).fetchall()
    return [r["id"] for r in rows]


async def search(db, embedder, q, mode, source_id, unread, starred, limit, offset, type_="", bookmarked=False,
                 source_ids="", types="") -> dict:
    q = (q or "").strip()
    if not q:
        return _recent(db, embedder.name, source_id, unread, starred, limit, offset, type_, bookmarked,
                       source_ids, types)
    conds, cond_params = _filter_clause(source_id, unread, starred, type_, bookmarked, source_ids, types)
    if mode == "semantic":
        query_vec = (await embedder.embed([q], is_query=True))[0]
        hits = knn_search(db, embedder.name, query_vec, limit * 3 + offset + 50)
        items = _load_items(db, embedder.name, [iid for iid, _ in hits], conds, cond_params)
        _attach_scores(
            items,
            {iid: max(0.0, 1.0 - (dist * dist) / 2.0) for iid, dist in hits},
        )
    else:
        kw_ids = keyword_ids(db, q)
        items = _load_items(db, embedder.name, kw_ids, conds, cond_params)
        if mode == "keyword":
            _attach_scores(
                items,
                {
                    iid: (len(kw_ids) - rank) / max(1, len(kw_ids))
                    for rank, iid in enumerate(kw_ids)
                },
            )
        else:
            query_vec = (await embedder.embed([q], is_query=True))[0]
            sem_hits = knn_search(db, embedder.name, query_vec, 200)
            sem_items = {
                d["id"]: d
                for d in _load_items(
                    db, embedder.name, [iid for iid, _ in sem_hits], conds, cond_params
                )
            }
            rrf: dict[int, float] = {}
            for rank, item in enumerate(items):
                rrf[item["id"]] = rrf.get(item["id"], 0.0) + 1.0 / (60 + rank)
            for rank, iid in enumerate([iid for iid, _ in sem_hits]):
                if iid in sem_items:
                    rrf[iid] = rrf.get(iid, 0.0) + 1.0 / (60 + rank)
            item_map = {d["id"]: d for d in items} | sem_items
            top = max(rrf.values()) if rrf else 0.0
            items = [item_map[i] for i in sorted(rrf, key=lambda k: rrf[k], reverse=True)]
            if top > 0:
                _attach_scores(items, {iid: s / top for iid, s in rrf.items()})
    has_more = len(items) > offset + limit
    return {"items": items[offset : offset + limit], "has_more": has_more, "mode": mode}
