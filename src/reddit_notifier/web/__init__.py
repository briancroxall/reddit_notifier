"""Local web UI: `uv run reddit-notifier web`, then open http://127.0.0.1:5050.

It only runs on this computer (127.0.0.1) and has no login. It shares the
SQLite database with the poller (`reddit-notifier run`), which keeps running
separately so alerts don't depend on this page being open.
"""

import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import db
from ..config import Config
from ..feed import FetchError, Post, fetch_posts
from ..matcher import Match, WatchItem, matching_posts
from ..notify import describe

HOST = "127.0.0.1"
# Reuse a fetched feed this long, so clicking "Check recent" on several items
# in a row costs Reddit one request instead of several.
FEED_CACHE_MINUTES = 5
# If Reddit refuses a fresh fetch, fall back to a cached feed up to this old.
STALE_FEED_MINUTES = 60


@dataclass
class FeedCache:
    posts: list[Post] = field(default_factory=list)
    fetched_at: str | None = None  # ISO timestamp, like the database's
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, cfg: Config) -> tuple[list[Post], str, str | None]:
        """(posts, fetched_at, problem). `problem` explains why older posts
        are being shown, or is None. Raises FetchError if there's nothing usable."""
        with self.lock:  # two quick clicks shouldn't mean two requests
            age = minutes_since(self.fetched_at) if self.fetched_at else None
            if age is not None and age < FEED_CACHE_MINUTES:
                return self.posts, self.fetched_at, None
            try:
                self.posts = fetch_posts(cfg.feed_url, cfg.user_agent)
                self.fetched_at = db.now()
                return self.posts, self.fetched_at, None
            except FetchError as e:
                if age is not None and age < STALE_FEED_MINUTES:
                    return self.posts, self.fetched_at, str(e)
                raise


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)
    # Only used to sign flash messages; there are no logins, so a fresh key
    # each start is fine.
    app.secret_key = secrets.token_hex()
    # Browsers share cookies across every port on 127.0.0.1, so use our own
    # name rather than Flask's default "session" (which Noted also uses).
    app.config["SESSION_COOKIE_NAME"] = "reddit_notifier_session"
    app.config["NOTIFIER"] = cfg
    db.set_display_timezone(cfg.timezone)
    feed_cache = app.extensions["feed_cache"] = FeedCache()

    @app.before_request
    def reject_cross_site_posts():
        # Stops other websites open in your browser from submitting our forms.
        # Browsers send Origin on form posts; tools like curl usually don't.
        origin = request.headers.get("Origin")
        if request.method == "POST" and origin and origin != request.host_url.rstrip("/"):
            abort(403)

    @app.teardown_appcontext
    def close_db(exc):
        if (conn := g.pop("conn", None)) is not None:
            conn.close()

    app.add_template_filter(db.localtime, "localtime")
    app.add_template_filter(timeago)
    app.add_template_global(match_details)

    @app.context_processor
    def nav_counts():
        return {"unread_count": db.count_unread(get_conn())}

    @app.route("/")
    def home():
        conn = get_conn()
        last = db.last_poll(conn)
        return render_template(
            "home.html",
            last=last,
            poller_stale=is_stale(last, cfg),
            polls=db.recent_polls(conn, limit=10),
            matches=db.recent_matches(conn, limit=50),
            cfg=cfg,
        )

    @app.get("/posts/<post_id>/open")
    def open_post(post_id):
        """Mark a matched post read, then go to it on Reddit."""
        conn = get_conn()
        url = db.match_url(conn, post_id)
        if url is None:
            abort(404)
        db.mark_read(conn, post_id)
        return redirect(url)

    @app.post("/posts/<post_id>/read")
    def mark_read(post_id):
        db.mark_read(get_conn(), post_id)
        return redirect(url_for("home"))

    @app.post("/matches/read-all")
    def mark_all_read():
        if db.mark_all_read(get_conn()):
            flash("Marked all matches as read.", "success")
        return redirect(url_for("home"))

    # --- watchlist -----------------------------------------------------------

    @app.get("/watchlist")
    def watchlist():
        return render_template("watchlist.html", items=db.list_watch_items(get_conn()))

    @app.route("/watchlist/new", methods=["GET", "POST"])
    def new_item():
        item, errors = WatchItem(name=""), []
        if request.method == "POST":
            item, errors = item_from_form(request.form)
            if not errors:
                db.add_watch_item(get_conn(), item)
                flash(f"Added {item.name}.", "success")
                return redirect(url_for("watchlist"))
        return render_template("item_form.html", item=item, errors=errors, is_new=True)

    @app.route("/watchlist/<int:item_id>/edit", methods=["GET", "POST"])
    def edit_item(item_id):
        conn = get_conn()
        item = db.get_watch_item(conn, item_id) or abort(404)
        errors = []
        if request.method == "POST":
            item, errors = item_from_form(request.form, item_id=item_id)
            if not errors:
                db.update_watch_item(conn, item)
                flash(f"Saved {item.name}.", "success")
                return redirect(url_for("watchlist"))
        return render_template("item_form.html", item=item, errors=errors, is_new=False)

    @app.post("/watchlist/<int:item_id>/delete")
    def delete_item(item_id):
        conn = get_conn()
        item = db.get_watch_item(conn, item_id) or abort(404)
        db.remove_watch_item(conn, item_id)
        # Past matches keep the item's name, so the home page history stays intact.
        flash(f"Deleted {item.name}.", "success")
        return redirect(url_for("watchlist"))

    # --- check recent posts ----------------------------------------------------
    # POST rather than GET, so nothing (a link preview, a prefetch) can make
    # a request to Reddit just by loading a page.

    @app.post("/check")
    def check_all():
        return check_recent(db.list_watch_items(get_conn()), label="your watchlist")

    @app.post("/watchlist/<int:item_id>/check")
    def check_item(item_id):
        item = db.get_watch_item(get_conn(), item_id) or abort(404)
        return check_recent([item], label=item.name, item=item)

    def check_recent(items, label, item=None):
        """Match items against the whole recent feed. Shows results only:
        never notifies or saves anything."""
        try:
            posts, fetched_at, problem = feed_cache.get(cfg)
        except FetchError as e:
            return render_template("check.html", label=label, item=item, error=str(e))
        return render_template(
            "check.html",
            label=label,
            item=item,
            results=matching_posts(posts, items),
            posts=posts,
            fetched_at=fetched_at,
            problem=problem,
            describe=describe,
        )

    return app


def lines(text: str) -> list[str]:
    """One entry per line: trimmed, blank lines dropped, duplicates removed."""
    entries = (line.strip() for line in text.splitlines())
    return list(dict.fromkeys(e for e in entries if e))


def item_from_form(form, item_id: int | None = None) -> tuple[WatchItem, list[str]]:
    """Build a WatchItem from the add/edit form, plus any problems to show.
    Checkboxes only appear in the form data when they're ticked."""
    item = WatchItem(
        id=item_id,
        name=form.get("name", "").strip(),
        variants=lines(form.get("variants", "")),
        excludes=lines(form.get("excludes", "")),
        sale_only="sale_only" in form,
        fuzzy="fuzzy" in form,
        notes=form.get("notes", "").strip(),
    )
    errors = []
    if not item.name:
        errors.append("Name is required.")
    if overlap := set(map(str.lower, item.terms)) & set(map(str.lower, item.excludes)):
        errors.append(
            "A phrase can't be both matched and excluded: " + ", ".join(sorted(overlap)) + "."
        )
    return item, errors


def get_conn():
    """One database connection per request, closed when the request ends."""
    if "conn" not in g:
        g.conn = db.connect(current_cfg().db_path)
    return g.conn


def current_cfg() -> Config:
    return current_app.config["NOTIFIER"]


def minutes_since(iso: str) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds() / 60


def is_stale(last_poll, cfg: Config) -> bool:
    """True if the poller hasn't logged anything for 3 intervals. Its longest
    backoff is 30 minutes, so allow at least that much before warning."""
    if last_poll is None:
        return True
    allowed = max(3 * cfg.poll_interval_minutes, 35)
    return minutes_since(last_poll["ran_at"]) > allowed


def timeago(iso: str | None) -> str:
    """ "just now", "4 min ago", "3 hr ago", or a date for anything older."""
    if not iso:
        return ""
    minutes = minutes_since(iso)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    if minutes < 24 * 60:
        return f"{minutes / 60:.0f} hr ago"
    return db.localtime(iso)


def match_details(row) -> str:
    """The same one-line summary the phone notification uses, from a saved match."""
    item = WatchItem(row["item_name"])
    match = Match(item, row["term"], row["matched_text"], bool(row["fuzzy"]), row["where_found"])
    details = describe(match)
    # describe() starts with the item name, which the table already shows.
    return details.removeprefix(row["item_name"]).strip(" ()")


def serve(cfg: Config, port: int | None = None) -> None:
    port = port or cfg.web_port
    print(f"Open http://{HOST}:{port} in your browser. Press Ctrl-C to stop.")
    # Never debug=True: Flask's debugger lets anyone who reaches the page run code.
    create_app(cfg).run(host=HOST, port=port, debug=False)
