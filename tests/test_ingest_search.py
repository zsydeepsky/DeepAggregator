import asyncio

from app.core.sources.base import RawItem
from app.core.services import search
from app.core.services.embeddings import embed_missing
from app.core.services.ingest import upsert_items
from conftest import FakeEmbedder


def make_source(db):
    cur = db.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES ('rss', 'test', 'https://example.com/feed')"
    )
    db.conn.commit()
    return cur.lastrowid


def raw_items():
    return [
        RawItem(
            guid="g1",
            title="CUDA GPU kernel optimization",
            summary="We speed up GPU kernels on modern hardware.",
            url="https://x.com/1",
        ),
        RawItem(
            guid="g2",
            title="红烧肉的做法",
            summary="五花肉、冰糖、料酒，小火慢炖两小时。",
            url="https://x.com/2",
        ),
        RawItem(
            guid="g3",
            title="Transformer attention survey",
            summary="A survey of attention mechanisms.",
            url="https://x.com/3",
        ),
    ]


def test_upsert_dedupe(db):
    source_id = make_source(db)
    assert upsert_items(db, source_id, raw_items()) == 3
    assert upsert_items(db, source_id, raw_items()) == 0


def test_keyword_search(db):
    make_source(db)
    upsert_items(db, 1, raw_items())
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "GPU kernel", "keyword", None, False, False, 10, 0)
    )
    assert result["items"][0]["title"] == "CUDA GPU kernel optimization"
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "红烧肉", "keyword", None, False, False, 10, 0)
    )
    assert result["items"][0]["title"] == "红烧肉的做法"


def test_semantic_and_hybrid(db):
    make_source(db)
    upsert_items(db, 1, raw_items())
    embedder = FakeEmbedder()
    assert asyncio.run(embed_missing(db, embedder, 10)) == 3
    assert asyncio.run(embed_missing(db, embedder, 10)) == 0
    result = asyncio.run(
        search.search(db, embedder, "kernels", "semantic", None, False, False, 10, 0)
    )
    assert len(result["items"]) == 3
    assert all(it["has_vector"] for it in result["items"])
    result = asyncio.run(
        search.search(db, embedder, "GPU", "hybrid", None, False, False, 10, 0)
    )
    assert {it["id"] for it in result["items"]} == {1, 2, 3}
    assert result["items"][0]["id"] == 1
    scores = [it.get("score") for it in result["items"]]
    assert all(s is not None and 0.0 <= s <= 1.0 for s in scores)
    result = asyncio.run(
        search.search(db, embedder, "GPU", "semantic", None, False, False, 10, 0)
    )
    assert all(0.0 <= it["score"] <= 1.0 for it in result["items"])


def test_type_filter(db):
    make_source(db)
    upsert_items(db, 1, raw_items())
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "", "hybrid", None, False, False, 10, 0, "rss")
    )
    assert len(result["items"]) == 3
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "", "hybrid", None, False, False, 10, 0, "youtube")
    )
    assert result["items"] == []


def test_bookmark_filter(db):
    make_source(db)
    upsert_items(db, 1, raw_items())
    db.conn.execute("UPDATE items SET is_bookmarked = 1 WHERE id = 2")
    db.conn.commit()
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "", "hybrid", None, False, False, 10, 0, "", True)
    )
    assert [it["id"] for it in result["items"]] == [2]
    assert result["items"][0]["is_bookmarked"] == 1


def test_source_ids_and_types_union_filter(db):
    # 两个不同类型的源：按 id 与按类型的过滤应取并集
    make_source(db)  # rss, id 1
    upsert_items(db, 1, raw_items())
    cur = db.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES ('reddit', 'r/test', '')"
    )
    reddit_id = cur.lastrowid
    reddit_items = [
        RawItem(guid=f"r{i}", title=f"r{i}", summary="s", url=f"u{i}") for i in range(1, 3)
    ]
    upsert_items(db, reddit_id, reddit_items)

    result = asyncio.run(
        search.search(db, FakeEmbedder(), "", "hybrid", None, False, False, 10, 0,
                      "", False, str(reddit_id), "rss")
    )
    # rss 源 3 条 + reddit 源 2 条，并集返回
    assert {it["source_id"] for it in result["items"]} == {1, 2}
    assert len(result["items"]) == 5


def test_filters_and_flags(db):
    make_source(db)
    upsert_items(db, 1, raw_items())
    db.conn.execute("UPDATE items SET is_read = 1 WHERE id = 1")
    db.conn.commit()
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "", "hybrid", None, True, False, 10, 0)
    )
    assert {it["id"] for it in result["items"]} == {2, 3}
    result = asyncio.run(
        search.search(db, FakeEmbedder(), "", "hybrid", 1, False, False, 10, 0)
    )
    assert len(result["items"]) == 3
    assert result["has_more"] is False
