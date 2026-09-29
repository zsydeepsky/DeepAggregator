import pytest

from app.core.workspaces import WorkspaceManager


def test_bootstrap_and_isolation(tmp_path):
    mgr = WorkspaceManager(tmp_path, initial="academic")
    assert mgr.active_name == "academic"
    db = mgr.current().db
    db.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES ('rss', 'paper', 'https://x')"
    )
    db.conn.commit()

    mgr.create("gaming")
    mgr.activate("gaming")
    assert mgr.active_name == "gaming"
    assert mgr.current().db.one("SELECT COUNT(*) AS c FROM sources")["c"] == 0

    mgr.activate("academic")
    assert mgr.current().db.one("SELECT COUNT(*) AS c FROM sources")["c"] == 1

    summaries = {s["name"]: s for s in mgr.summaries()}
    assert set(summaries) == {"academic", "gaming"}
    assert summaries["academic"]["active"] is True
    assert summaries["academic"]["sources"] == 1


def test_pointer_persists_across_managers(tmp_path):
    WorkspaceManager(tmp_path, initial="default")
    mgr2 = WorkspaceManager(tmp_path)
    mgr2.create("manga")
    mgr2.activate("manga")

    mgr3 = WorkspaceManager(tmp_path)
    assert mgr3.active_name == "manga"


def test_invalid_names(tmp_path):
    mgr = WorkspaceManager(tmp_path)
    with pytest.raises(ValueError):
        mgr.create("../evil")
    with pytest.raises(ValueError):
        mgr.create(".hidden")
    with pytest.raises(ValueError):
        WorkspaceManager(tmp_path, initial="a/b")


def test_activate_missing(tmp_path):
    mgr = WorkspaceManager(tmp_path)
    with pytest.raises(KeyError):
        mgr.activate("nope")


def test_legacy_layout_migrates_to_default(tmp_path):
    from app.core.db import Database

    legacy = Database(tmp_path / "aggregator.db")
    legacy.init()
    legacy.conn.execute(
        "INSERT INTO sources(type, name, url) VALUES ('rss', 'old', 'https://x')"
    )
    legacy.conn.commit()
    legacy.conn.close()

    mgr = WorkspaceManager(tmp_path)
    assert mgr.active_name == "default"
    assert not (tmp_path / "aggregator.db").exists()
    assert mgr.current().db.one("SELECT COUNT(*) AS c FROM sources")["c"] == 1
