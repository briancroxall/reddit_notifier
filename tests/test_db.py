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
