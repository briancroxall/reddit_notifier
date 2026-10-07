"""Polling tests with a fake feed and a fake notifier: no network, no phone."""

from pathlib import Path

import pytest

from reddit_notifier import cli, db
from reddit_notifier.config import Config
from reddit_notifier.feed import FetchError, Post
from reddit_notifier.matcher import WatchItem
from reddit_notifier.notify import NotifyError


def make_post(n, title, body=""):
    return Post(f"t3_{n}", title, body, f"https://reddit.com/{n}", "/u/someone", "")


@pytest.fixture
def cfg(tmp_path):
    return Config("fragranceswap", 5, "ua", tmp_path / "test.db", "https://ntfy.example", "topic")


@pytest.fixture
def conn(cfg):
    conn = db.connect(cfg.db_path)
    db.add_watch_item(conn, WatchItem("Viride", sale_only=True))
    return conn


@pytest.fixture
def world(monkeypatch):
    """A fake feed (edit world.posts, newest first) and a record of notifications."""

    class World:
        posts: list[Post] = [make_post(1, "[WTS] Old Viride listing")]
        notified: list[tuple[Post, list]] = []
        notify_fails = False

    w = World()
    w.notified = []

    def fake_fetch(url, user_agent):
        return list(w.posts)

    def fake_notify(cfg, post, matches):
        if w.notify_fails:
            raise NotifyError("ntfy is down")
        w.notified.append((post, matches))

    monkeypatch.setattr(cli, "fetch_posts", fake_fetch)
    monkeypatch.setattr(cli, "notify_post", fake_notify)
    return w


def test_first_poll_seeds_without_notifying(cfg, conn, world):
    cli.poll_once(cfg, conn)
    assert world.notified == []
    assert db.unseen(conn, ["t3_1"]) == set()
    assert db.last_poll(conn)["status"] == "seeded"


def test_new_matching_post_notifies_once(cfg, conn, world):
    cli.poll_once(cfg, conn)  # seed
    world.posts = [
        make_post(3, "[WTT] Viride for trade"),  # sale-only item: skipped
        make_post(2, "[WTS] Lot", "Viride 100ml"),
        *world.posts,
    ]
    cli.poll_once(cfg, conn)
    assert [p.id for p, _ in world.notified] == ["t3_2"]
    assert [row["post_id"] for row in db.recent_matches(conn)] == ["t3_2"]

    cli.poll_once(cfg, conn)  # same feed again: nothing new
    assert len(world.notified) == 1


def dry_run(cfg, conn):
    args = cli.build_parser().parse_args(["poll", "--dry-run"])
    args.func(args, cfg, conn)


def test_dry_run_on_empty_database_writes_nothing(cfg, conn, world, capsys):
    dry_run(cfg, conn)
    assert "[WTS] Old Viride listing" in capsys.readouterr().out
    assert world.notified == []
    assert db.has_seen_any(conn) is False
    assert db.last_poll(conn) is None


def test_failed_notification_is_retried(cfg, conn, world):
    cli.poll_once(cfg, conn)  # seed
    world.posts = [make_post(2, "[WTS] Viride"), *world.posts]

    world.notify_fails = True
    cli.poll_once(cfg, conn)
    assert db.unseen(conn, ["t3_2"]) == {"t3_2"}  # still unseen
    assert db.last_poll(conn)["status"] == "error"

    world.notify_fails = False
    cli.poll_once(cfg, conn)
    assert [p.id for p, _ in world.notified] == ["t3_2"]


def test_dry_run_includes_seen_posts_and_writes_nothing(cfg, conn, world, capsys):
    cli.poll_once(cfg, conn)  # seed: the Viride post is now "seen"
    before = db.recent_matches(conn), db.last_poll(conn)["id"]

    dry_run(cfg, conn)

    out = capsys.readouterr().out
    assert "[WTS] Old Viride listing" in out
    assert "Checked 1 recent posts" in out
    assert world.notified == []
    assert (db.recent_matches(conn), db.last_poll(conn)["id"]) == before


def test_fetch_error_propagates(cfg, conn, monkeypatch):
    def broken(url, user_agent):
        raise FetchError("offline")

    monkeypatch.setattr(cli, "fetch_posts", broken)
    with pytest.raises(FetchError):
        cli.poll_once(cfg, conn)


def test_next_wait(cfg):
    assert cli.next_wait(cfg, failures=0) == 300
    assert cli.next_wait(cfg, failures=1) == 600
    assert cli.next_wait(cfg, failures=2) == 1200
    assert cli.next_wait(cfg, failures=5) == 1800  # capped at 30 min
    assert cli.next_wait(cfg, failures=1, retry_after=900) == 900
    assert cli.next_wait(cfg, failures=1, retry_after=99999) == 1800


def test_watch_commands(cfg, capsys):
    conn = db.connect(cfg.db_path)
    parser = cli.build_parser()

    a = parser.parse_args(
        ["watch", "add", "Aventus", "--exclude", "Aventus Cologne", "--sale-only", "--notes", "100ml"]
    )
    a.func(a, cfg, conn)
    [item] = db.list_watch_items(conn)
    assert item.excludes == ["Aventus Cologne"] and item.sale_only and item.fuzzy

    a = parser.parse_args(["watch", "list"])
    a.func(a, cfg, conn)
    out = capsys.readouterr().out
    assert "Aventus  [sale only, fuzzy]" in out
    assert "excludes: Aventus Cologne" in out

    a = parser.parse_args(["watch", "remove", str(item.id)])
    a.func(a, cfg, conn)
    assert db.list_watch_items(conn) == []
