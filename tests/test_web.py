"""Web UI tests use Flask's test client and a temporary database."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from reddit_notifier import db, web
from reddit_notifier.config import Config
from reddit_notifier.feed import Post, RateLimited
from reddit_notifier.matcher import Match, WatchItem
from reddit_notifier.web import create_app, match_details


@pytest.fixture
def cfg(tmp_path):
    return Config("fragranceswap", 5, "ua", tmp_path / "test.db", "https://ntfy.example", "topic")


@pytest.fixture
def conn(cfg):
    return db.connect(cfg.db_path)


@pytest.fixture
def client(cfg):
    return create_app(cfg).test_client()


def log_poll_at(conn, minutes_ago, status="ok", error=None):
    ran_at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO poll_log (ran_at, status, n_posts, n_new, error) VALUES (?, ?, 100, 2, ?)",
            (ran_at, status, error),
        )


def page(client, url="/"):
    response = client.get(url)
    assert response.status_code == 200
    return response.get_data(as_text=True)


# --- home page ---------------------------------------------------------------


def test_home_before_any_polls(client):
    html = page(client)
    assert "The poller hasn't run yet." in html
    assert "No matches yet." in html


def test_home_with_recent_poll(client, conn):
    log_poll_at(conn, minutes_ago=3)
    html = page(client)
    assert "3 min ago" in html
    assert "may not be running" not in html
    assert "2 new of 100 posts" in html


def test_home_warns_when_poller_stale(client, conn):
    log_poll_at(conn, minutes_ago=90)
    assert "It may not be running." in page(client)


def test_home_shows_poll_error(client, conn):
    log_poll_at(conn, minutes_ago=1, status="error", error="Rate limited by Reddit (HTTP 429)")
    html = page(client)
    assert "badge-error" in html
    assert "Rate limited by Reddit (HTTP 429)" in html


def add_match(conn, post_id="t3_x", item_name="Viride"):
    item = WatchItem(item_name, id=db.add_watch_item(conn, WatchItem(item_name)))
    post = SimpleNamespace(id=post_id, title=f"[WTS] {item_name} 100ml", url=f"https://reddit.com/{post_id}")
    db.record_match(conn, post, Match(item, item_name, "virdie", True, "body"), notified=True)


def test_home_lists_matches(client, conn):
    add_match(conn)
    html = page(client)
    assert 'href="/posts/t3_x/open"' in html
    assert "[WTS] Viride 100ml" in html
    assert "fuzzy: &#34;virdie&#34;, in body" in html


# --- read / unread -------------------------------------------------------------


def test_new_match_shows_as_unread(client, conn):
    add_match(conn)
    html = page(client)
    assert 'id="nav-unread"> (1)<' in html  # navbar count
    assert 'class="unread"' in html
    assert "Mark read" in html


def test_opening_a_post_marks_it_read(client, conn):
    add_match(conn)
    response = client.get("/posts/t3_x/open")
    assert response.status_code == 302
    assert response.location == "https://reddit.com/t3_x"
    assert db.count_unread(conn) == 0
    assert 'id="nav-unread"><' in page(client)  # count gone


def test_open_unknown_post_is_404(client):
    assert client.get("/posts/t3_nope/open").status_code == 404


def test_mark_read_covers_every_item_on_the_post(client, conn):
    add_match(conn, "t3_x", "Viride")
    add_match(conn, "t3_x", "Orage")  # same post, second item
    add_match(conn, "t3_y", "Aventus")
    assert db.count_unread(conn) == 2  # counted by post

    client.post("/posts/t3_x/read")
    assert db.count_unread(conn) == 1


def test_mark_all_read(client, conn):
    add_match(conn, "t3_x")
    add_match(conn, "t3_y", "Orage")
    response = client.post("/matches/read-all", follow_redirects=True)
    assert "Marked all matches as read." in response.get_data(as_text=True)
    assert db.count_unread(conn) == 0


def test_home_keeps_only_a_few_read_posts(cfg, conn):
    cfg.read_matches_on_home = 2
    client = create_app(cfg).test_client()
    for n in range(4):
        add_match(conn, f"t3_read{n}", f"Item{n}")
        db.mark_read(conn, f"t3_read{n}")
    add_match(conn, "t3_new", "Fresh")

    html = page(client)
    assert "t3_new" in html  # unread always shown
    assert "t3_read3" in html and "t3_read2" in html  # the 2 read most recently
    assert "t3_read1" not in html and "t3_read0" not in html
    assert html.index("t3_new") < html.index("t3_read3")  # unread first
    assert 'href="/history"' in html


def test_home_read_limit_counts_posts_not_items(cfg, conn):
    cfg.read_matches_on_home = 1
    client = create_app(cfg).test_client()
    add_match(conn, "t3_old", "Aventus")
    add_match(conn, "t3_x", "Viride")
    add_match(conn, "t3_x", "Orage")  # same post, second item
    db.mark_all_read(conn)
    html = page(client)
    assert "[WTS] Viride" in html and "[WTS] Orage" in html
    assert "t3_old" not in html


def test_history_shows_read_and_unread(client, conn):
    add_match(conn, "t3_x")
    add_match(conn, "t3_y", "Orage")
    db.mark_read(conn, "t3_x")
    html = page(client, "/history")
    assert "t3_x" in html and "t3_y" in html
    assert "2 in all" in html


def test_history_pages(client, conn, monkeypatch):
    monkeypatch.setattr(web, "MATCHES_PER_PAGE", 2)
    for n in range(5):
        add_match(conn, f"t3_p{n}", f"Item{n}")
    first = page(client, "/history")
    assert "t3_p4" in first and "t3_p2" not in first
    assert "Page 1 of 3" in first and "?page=2" in first
    last = page(client, "/history?page=99")  # past the end shows the last page
    assert "t3_p0" in last and "Page 3 of 3" in last


def test_mark_read_returns_to_the_page_it_was_on(client, conn):
    add_match(conn)
    response = client.post("/posts/t3_x/read", data={"next": "/history?page=2"})
    assert response.location == "/history?page=2"
    response = client.post("/posts/t3_x/read", data={"next": "//evil.example"})
    assert response.location == "/"


def test_match_details_mirrors_notification_text():
    row = {"item_name": "Viride", "term": "Vi ride", "matched_text": "vi ride", "fuzzy": 0, "where_found": "title"}
    assert match_details(row) == 'as "Vi ride"'
    row = {**row, "term": "Viride", "matched_text": "viride"}
    assert match_details(row) == ""


# --- safety ------------------------------------------------------------------


def test_session_cookie_name_does_not_clash(cfg):
    assert create_app(cfg).config["SESSION_COOKIE_NAME"] == "reddit_notifier_session"


def test_post_from_another_site_is_rejected(client):
    response = client.post("/", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_home_has_hooks_for_instant_read_update(client, conn):
    add_match(conn)
    html = page(client)
    assert 'data-post-id="t3_x"' in html
    assert 'id="nav-unread"' in html
    assert "matches.js" in html
    assert client.get("/static/matches.js").status_code == 200


# --- watchlist -----------------------------------------------------------------


def form(**overrides):
    data = {"name": "Viride", "variants": "", "excludes": "", "notes": "", "fuzzy": "on"}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}  # None = unticked checkbox


def test_empty_watchlist(client):
    assert "Your watchlist is empty." in page(client, "/watchlist")


def test_new_item_form_defaults_to_fuzzy(client):
    html = page(client, "/watchlist/new")
    assert 'name="fuzzy" checked' in html
    assert 'name="sale_only" >' in html  # unticked


def test_add_item(client, conn):
    response = client.post(
        "/watchlist/new",
        data=form(
            name="  Mousse de Chene 30 ",
            variants="MDC 30\n\n  MdC30  \nMDC 30\n",  # blank and duplicate lines dropped
            excludes="Mousse de Chene 30 Body Lotion",
            sale_only="on",
            notes="50ml+",
        ),
        follow_redirects=True,
    )
    assert "Added Mousse de Chene 30." in response.get_data(as_text=True)
    [item] = db.list_watch_items(conn)
    assert item.name == "Mousse de Chene 30"
    assert item.variants == ["MDC 30", "MdC30"]
    assert item.excludes == ["Mousse de Chene 30 Body Lotion"]
    assert item.sale_only and item.fuzzy
    assert item.notes == "50ml+"


def test_add_item_requires_name_and_keeps_input(client, conn):
    response = client.post("/watchlist/new", data=form(name="  ", variants="MDC 30"))
    html = response.get_data(as_text=True)
    assert "Name is required." in html
    assert "MDC 30</textarea>" in html  # what you typed is still there
    assert db.list_watch_items(conn) == []


def test_cannot_exclude_the_name_itself(client, conn):
    response = client.post("/watchlist/new", data=form(excludes="viride"))
    assert "can&#39;t be both matched and excluded: viride." in response.get_data(as_text=True)
    assert db.list_watch_items(conn) == []


def test_edit_item(client, conn):
    item_id = db.add_watch_item(conn, WatchItem("Viride", variants=["Vi ride"], sale_only=True))
    html = page(client, f"/watchlist/{item_id}/edit")
    assert "Edit Viride" in html
    assert "Vi ride</textarea>" in html

    client.post(f"/watchlist/{item_id}/edit", data=form(name="Viride", fuzzy=None))
    item = db.get_watch_item(conn, item_id)
    assert item.variants == []
    assert item.sale_only is False  # checkbox left unticked
    assert item.fuzzy is False


def test_edit_missing_item_is_404(client):
    assert client.get("/watchlist/999/edit").status_code == 404


def test_delete_item_keeps_past_matches(client, conn):
    add_match(conn)  # creates the Viride item and a match for it
    [item] = db.list_watch_items(conn)
    response = client.post(f"/watchlist/{item.id}/delete", follow_redirects=True)
    assert "Deleted Viride." in response.get_data(as_text=True)
    assert db.list_watch_items(conn) == []
    assert "[WTS] Viride 100ml" in page(client)  # still on the Matches page


def test_delete_confirm_text_is_safely_quoted(client, conn):
    item_id = db.add_watch_item(conn, WatchItem("L'Homme <Intense>"))
    html = page(client, f"/watchlist/{item_id}/edit")
    assert "confirm(\"Delete L\\u0027Homme \\u003cIntense\\u003e from your watchlist?\")" in html


def test_watchlist_lists_items(client, conn):
    db.add_watch_item(conn, WatchItem("Aventus", excludes=["Aventus Cologne"], sale_only=True, fuzzy=False))
    html = page(client, "/watchlist")
    assert "Aventus Cologne" in html
    assert "[WTS] posts only" in html
    assert "Exact spelling only" in html


# --- check recent posts ----------------------------------------------------------


def feed_post(n, title, body="", hours_ago=1):
    published = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return Post(f"t3_{n}", title, body, f"https://reddit.com/{n}", "/u/someone", published)


@pytest.fixture
def feed(monkeypatch):
    """A fake Reddit feed. Set feed.error to make fetching fail."""
    state = SimpleNamespace(
        posts=[
            feed_post(1, "[WTS] Viride 100ml", hours_ago=1),
            feed_post(2, "[WTT] Aventus", hours_ago=2),
            feed_post(3, "[WTS] Lot", "Orage, Virdie decant", hours_ago=3),
        ],
        fetches=0,
        error=None,
    )

    def fake_fetch(url, user_agent):
        state.fetches += 1
        if state.error:
            raise state.error
        return list(state.posts)

    monkeypatch.setattr(web, "fetch_posts", fake_fetch)
    return state


def test_check_all(client, conn, feed):
    db.add_watch_item(conn, WatchItem("Viride"))
    db.add_watch_item(conn, WatchItem("Aventus", sale_only=True))
    html = client.post("/check").get_data(as_text=True)

    assert "Matching your watchlist" in html
    assert "nothing was notified or saved" in html
    assert "[WTS] Viride 100ml" in html
    assert "[WTS] Lot" in html  # fuzzy match in the body
    assert "Viride (fuzzy: &#34;virdie&#34;, in body)" in html
    assert "[WTT] Aventus" not in html  # sale-only item, trade post
    assert "Checked the 3 newest posts" in html


def test_check_one_item(client, conn, feed):
    db.add_watch_item(conn, WatchItem("Viride"))
    orage_id = db.add_watch_item(conn, WatchItem("Orage"))
    html = client.post(f"/watchlist/{orage_id}/check").get_data(as_text=True)
    assert "Matching Orage" in html
    assert "[WTS] Lot" in html
    assert "[WTS] Viride 100ml" not in html


def test_check_with_no_results(client, conn, feed):
    item_id = db.add_watch_item(conn, WatchItem("Mousse de Chene 30"))
    html = client.post(f"/watchlist/{item_id}/check").get_data(as_text=True)
    assert "No recent posts match Mousse de Chene 30." in html


def test_check_writes_nothing(client, conn, feed):
    db.add_watch_item(conn, WatchItem("Viride"))
    client.post("/check")
    assert db.recent_matches(conn) == []
    assert db.has_seen_any(conn) is False
    assert db.last_poll(conn) is None


def test_checks_reuse_the_feed(client, conn, feed):
    item_id = db.add_watch_item(conn, WatchItem("Viride"))
    client.post("/check")
    client.post(f"/watchlist/{item_id}/check")
    client.post("/check")
    assert feed.fetches == 1


def test_check_fetches_again_after_cache_expires(cfg, conn, feed):
    app = create_app(cfg)
    client = app.test_client()
    client.post("/check")
    cache = app.extensions["feed_cache"]
    cache.fetched_at = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    client.post("/check")
    assert feed.fetches == 2


def test_check_falls_back_to_older_feed_when_rate_limited(cfg, conn, feed):
    db.add_watch_item(conn, WatchItem("Viride"))
    app = create_app(cfg)
    client = app.test_client()
    client.post("/check")
    app.extensions["feed_cache"].fetched_at = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()

    feed.error = RateLimited(None)
    html = client.post("/check").get_data(as_text=True)
    assert "Reddit didn't answer just now" in html
    assert "[WTS] Viride 100ml" in html  # still shows the older results


def test_check_error_with_nothing_cached(client, conn, feed):
    feed.error = RateLimited(None)
    html = client.post("/check").get_data(as_text=True)
    assert "Couldn't get recent posts from Reddit" in html
    assert "Rate limited by Reddit (HTTP 429)" in html


def test_check_unknown_item_is_404(client, feed):
    assert client.post("/watchlist/999/check").status_code == 404
    assert feed.fetches == 0


def test_check_buttons_are_forms(client, conn):
    db.add_watch_item(conn, WatchItem("Viride"))
    html = page(client, "/watchlist")
    assert '<form method="post" action="/check">' in html  # navbar
    assert 'action="/watchlist/1/check"' in html
