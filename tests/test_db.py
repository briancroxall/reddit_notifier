from types import SimpleNamespace

import pytest

from reddit_notifier import db
from reddit_notifier.matcher import Match, WatchItem


@pytest.fixture
def conn(tmp_path):
    # A fresh database in a temporary folder for each test.
    return db.connect(tmp_path / "test.db")


def test_watch_items_round_trip(conn):
    item = WatchItem(
        "Virēre",
        variants=["Virere", "Vi rere"],
        excludes=["Virēre Intense"],
        sale_only=True,
        fuzzy=False,
        notes="50ml ok",
    )
    item_id = db.add_watch_item(conn, item)

    [loaded] = db.list_watch_items(conn)
    assert loaded.id == item_id
    assert loaded.name == "Virēre"
    assert loaded.variants == ["Virere", "Vi rere"]
    assert loaded.excludes == ["Virēre Intense"]
    assert loaded.sale_only is True
    assert loaded.fuzzy is False
    assert loaded.notes == "50ml ok"


def test_remove_watch_item(conn):
    item_id = db.add_watch_item(conn, WatchItem("Viride"))
    assert db.remove_watch_item(conn, item_id) is True
    assert db.remove_watch_item(conn, item_id) is False  # already gone
    assert db.list_watch_items(conn) == []


def test_seen_posts(conn):
    assert db.has_seen_any(conn) is False
    db.mark_seen(conn, ["t3_a", "t3_b"])
    assert db.has_seen_any(conn) is True
    assert db.unseen(conn, ["t3_a", "t3_b", "t3_c"]) == {"t3_c"}
    db.mark_seen(conn, ["t3_a"])  # marking twice is harmless
    assert db.unseen(conn, []) == set()  # empty feed


def test_match_survives_item_deletion(conn):
    item = WatchItem("Viride")
    item.id = db.add_watch_item(conn, item)
    post = SimpleNamespace(id="t3_x", title="[WTS] Viride", url="https://example.com")
    db.record_match(conn, post, Match(item, "Viride", "viride", False, "title"), notified=True)

    db.remove_watch_item(conn, item.id)
    [row] = db.recent_matches(conn)
    assert row["item_name"] == "Viride"
    assert row["watch_item_id"] is None


def test_poll_log(conn):
    assert db.last_poll(conn) is None
    db.log_poll(conn, "seeded", n_posts=100)
    db.log_poll(conn, "ok", n_posts=100, n_new=3)
    last = db.last_poll(conn)
    assert last["status"] == "ok"
    assert last["n_new"] == 3


def test_get_and_update_watch_item(conn):
    item_id = db.add_watch_item(conn, WatchItem("Viride"))
    item = db.get_watch_item(conn, item_id)
    assert item.name == "Viride"

    item.variants = ["Vi ride"]
    item.excludes = ["Viride Intense"]
    item.sale_only = True
    item.fuzzy = False
    item.notes = "edited"
    assert db.update_watch_item(conn, item) is True
    assert db.get_watch_item(conn, item_id) == item

    assert db.get_watch_item(conn, 999) is None
    assert db.update_watch_item(conn, WatchItem("Ghost", id=999)) is False


def test_recent_polls_newest_first(conn):
    for status in ["seeded", "ok", "error"]:
        db.log_poll(conn, status)
    assert [row["status"] for row in db.recent_polls(conn, limit=2)] == ["error", "ok"]


def test_localtime():
    assert db.localtime(None) == ""
    # The exact text depends on this computer's time zone; check its shape.
    assert db.localtime("2026-10-06T20:00:00+00:00").startswith("Oct ")


def test_upgrade_adds_read_at_to_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE matches (id INTEGER PRIMARY KEY, post_id TEXT NOT NULL,"
        " watch_item_id INTEGER, item_name TEXT NOT NULL, term TEXT NOT NULL,"
        " matched_text TEXT NOT NULL, fuzzy INTEGER NOT NULL, where_found TEXT NOT NULL,"
        " title TEXT NOT NULL, url TEXT NOT NULL, notified INTEGER NOT NULL,"
        " matched_at TEXT NOT NULL)"
    )
    old.execute(
        "INSERT INTO matches VALUES (1, 't3_x', NULL, 'Viride', 'Viride', 'viride', 0,"
        " 'title', '[WTS] Viride', 'https://reddit.com/x', 1, '2026-10-06T20:00:00+00:00')"
    )
    old.commit()
    old.close()

    conn = db.connect(path)  # runs the upgrade
    [row] = db.recent_matches(conn)
    assert row["read_at"] is None
    assert db.count_unread(conn) == 1
    db.connect(path)  # running it again is harmless


def test_config_paths_are_relative_to_the_config_file(tmp_path, monkeypatch):
    from reddit_notifier.config import load_config

    (tmp_path / "config.toml").write_text('[ntfy]\ntopic = "t"\n')
    monkeypatch.chdir("/")  # run from somewhere else entirely
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.db_path == tmp_path.resolve() / "data" / "notifier.db"
    assert cfg.poll_interval_minutes == 10
    assert cfg.alert_after_minutes == 60
    assert cfg.feed_url == "https://www.reddit.com/r/fragranceswap/new/.rss?limit=100"


def test_config_feed_url_override_and_absolute_db_path(tmp_path):
    from reddit_notifier.config import load_config

    (tmp_path / "config.toml").write_text(
        'feed_url = "http://127.0.0.1:9/feed"\n'
        'db_path = "/tmp/elsewhere.db"\n'
        'alert_after_minutes = 5\n'
        '[ntfy]\ntopic = "t"\n'
    )
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.feed_url == "http://127.0.0.1:9/feed"
    assert str(cfg.db_path) == "/tmp/elsewhere.db"
    assert cfg.alert_after_minutes == 5
