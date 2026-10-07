"""Command-line interface: `uv run reddit-notifier --help`."""

import argparse
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import db
from .config import DEFAULT_PATH, Config, ConfigError, load_config
from .feed import FetchError, RateLimited, fetch_posts
from .matcher import WatchItem, match_post, matching_posts
from .notify import NotifyError, describe, notify_post, send, send_test

MAX_WAIT_SECONDS = 30 * 60


def say(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# --- polling -----------------------------------------------------------------


def poll_once(cfg: Config, conn) -> None:
    """Fetch the feed once, then notify about new matching posts.

    The first poll on an empty database only records what's already there,
    so you aren't flooded with alerts for old posts.
    Raises FetchError / RateLimited if the feed can't be fetched.
    """
    posts = fetch_posts(cfg.feed_url, cfg.user_agent)
    ids = [p.id for p in posts]

    if not db.has_seen_any(conn):
        db.mark_seen(conn, ids)
        db.log_poll(conn, "seeded", n_posts=len(posts))
        say(f"First run: recorded {len(posts)} existing posts. Only newer posts will alert you.")
        return

    new_ids = db.unseen(conn, ids)
    # The feed is newest-first; handle posts in the order they were made.
    new_posts = [p for p in reversed(posts) if p.id in new_ids]

    items = db.list_watch_items(conn)
    if not items:
        say("Your watchlist is empty. Add something with: reddit-notifier watch add NAME")

    done, errors, n_matched = [], [], 0
    for post in new_posts:
        matches = match_post(post, items)
        if matches:
            n_matched += 1
            summary = "; ".join(describe(m) for m in matches)
            try:
                notify_post(cfg, post, matches)
            except NotifyError as e:
                # Leave the post unseen so the next poll tries again.
                errors.append(f"{post.id}: {e}")
                say(f"Couldn't notify about {post.title!r}: {e}")
                continue
            for m in matches:
                db.record_match(conn, post, m, notified=True)
            say(f"Notified: {post.title}  ({summary})")
        done.append(post.id)

    db.mark_seen(conn, done)
    db.log_poll(
        conn,
        "error" if errors else "ok",
        n_posts=len(posts),
        n_new=len(new_posts),
        error="\n".join(errors) or None,
    )
    say(f"Checked {len(posts)} posts: {len(new_posts)} new, {n_matched} matched.")


def next_wait(cfg: Config, failures: int, retry_after: float | None = None) -> float:
    """Seconds to wait before the next poll.

    After a success, the normal interval. After failures, back off: 2x, 4x,
    8x the interval... capped at 30 minutes. If Reddit said how long to wait
    (Retry-After), wait at least that long.
    """
    interval = cfg.poll_interval_minutes * 60
    wait = interval * (2**failures) if failures else interval
    if retry_after:
        wait = max(wait, retry_after)
    return min(wait, MAX_WAIT_SECONDS)


@dataclass
class Alert:
    title: str
    message: str
    tags: list[str]


class TroubleMonitor:
    """Decides when to tell the phone that polling is failing, and when it
    has recovered. One alert per stretch of trouble, not one per failure.
    Brief problems (a single 429) never alert."""

    def __init__(self, alert_after_minutes: float):
        self.alert_after_minutes = alert_after_minutes
        self.since: datetime | None = None  # when the current trouble began
        self.alerted = False

    def failed(self, error: str, now: datetime) -> Alert | None:
        if self.since is None:
            self.since = now
        minutes = (now - self.since).total_seconds() / 60
        if self.alerted or minutes < self.alert_after_minutes:
            return None
        self.alerted = True
        return Alert(
            "Reddit notifier is having trouble",
            f"Checks have been failing for {minutes:.0f} minutes"
            f" (since {db.localtime(self.since.isoformat())})."
            f" Latest error: {error}\nIt will keep retrying.",
            ["warning"],
        )

    def succeeded(self, now: datetime) -> Alert | None:
        since, alerted = self.since, self.alerted
        self.since, self.alerted = None, False
        if not alerted:
            return None
        minutes = (now - since).total_seconds() / 60
        return Alert(
            "Reddit notifier is working again",
            f"Checks are succeeding again after {minutes:.0f} minutes of trouble.",
            ["white_check_mark"],
        )


def send_alert(cfg: Config, alert: Alert | None) -> None:
    if alert is None:
        return
    say(f"{alert.title}. {alert.message}")
    try:
        send(cfg, alert.title, alert.message, tags=alert.tags)
    except Exception as e:  # never let an alert stop the watcher
        say(f"Couldn't send that alert: {e}")


def run_forever(cfg: Config, conn) -> None:
    say(
        f"Watching r/{cfg.subreddit} every {cfg.poll_interval_minutes:g} minutes."
        " Press Ctrl-C to stop."
    )
    monitor = TroubleMonitor(cfg.alert_after_minutes)
    failures = 0
    while True:
        retry_after = None
        error = None
        try:
            poll_once(cfg, conn)
            failures = 0
        except RateLimited as e:
            retry_after = e.retry_after
            error = str(e)
            say(error)
        except FetchError as e:
            error = str(e)
            say(f"Couldn't fetch the feed: {e}")
        except Exception as e:
            # A bug shouldn't stop the watcher for good; log it and keep going.
            error = f"Unexpected error: {e}"
            say("Unexpected error:\n" + traceback.format_exc())

        now = datetime.now(UTC)
        if error is None:
            send_alert(cfg, monitor.succeeded(now))
        else:
            failures += 1
            db.log_poll(conn, "error", error=error)
            send_alert(cfg, monitor.failed(error, now))

        wait = next_wait(cfg, failures, retry_after)
        if failures:
            say(f"Trying again in {wait / 60:.0f} minutes.")
        time.sleep(wait)


# --- commands ----------------------------------------------------------------


def check_recent(cfg: Config, conn) -> None:
    """Dry run: match the watchlist against everything in the feed (about the
    last 12 hours), seen or not. Prints only: never notifies or saves."""
    posts = fetch_posts(cfg.feed_url, cfg.user_agent)
    results = matching_posts(posts, db.list_watch_items(conn))
    for post, matches in results:
        summary = "; ".join(describe(m) for m in matches)
        print(f"{db.localtime(post.published)}  {post.title}\n    {summary}\n    {post.url}")
    since = db.localtime(posts[-1].published) if posts else "?"
    print(
        f"Checked {len(posts)} recent posts (since {since}): {len(results)} matched."
        " Nothing was notified or saved."
    )


def cmd_poll(args, cfg, conn):
    try:
        if args.dry_run:
            check_recent(cfg, conn)
        else:
            poll_once(cfg, conn)
    except FetchError as e:
        if not args.dry_run:
            db.log_poll(conn, "error", error=str(e))
        sys.exit(f"Couldn't fetch the feed: {e}")


def cmd_run(args, cfg, conn):
    try:
        run_forever(cfg, conn)
    except KeyboardInterrupt:
        say("Stopped.")


def cmd_watch_add(args, cfg, conn):
    item = WatchItem(
        name=args.name,
        variants=args.variant,
        excludes=args.exclude,
        sale_only=args.sale_only,
        fuzzy=not args.no_fuzzy,
        notes=args.notes,
    )
    item.id = db.add_watch_item(conn, item)
    print(f"Added #{item.id}:")
    print(format_item(item))


def cmd_watch_list(args, cfg, conn):
    items = db.list_watch_items(conn)
    if not items:
        print("Watchlist is empty.")
    for item in items:
        print(format_item(item))


def cmd_watch_remove(args, cfg, conn):
    if db.remove_watch_item(conn, args.id):
        print(f"Removed #{args.id}.")
    else:
        sys.exit(f"No watch item #{args.id}. See: reddit-notifier watch list")


def cmd_web(args, cfg, conn):
    from .web import serve  # imported here so other commands don't load Flask

    conn.close()  # the web app opens its own connection for each page
    serve(cfg, port=args.port)


def cmd_service(args, cfg, conn):
    from . import service

    try:
        if args.action in ("start", "restart"):
            lines = service.start(cfg, args.config)
        elif args.action == "stop":
            lines = service.stop()
        elif args.action == "status":
            lines = service.status(cfg, conn)
        else:
            lines = service.logs(args.lines)
    except service.ServiceError as e:
        sys.exit(str(e))
    print("\n".join(lines))


def tunnel_command(cfg: Config) -> list[str]:
    return [
        "ssh", "-N",  # -N: forward the port only; don't open a shell
        "-L", f"{cfg.tunnel_port}:127.0.0.1:{cfg.web_port}",
        "-o", "ExitOnForwardFailure=yes",  # fail loudly if the local port is taken
        "-o", "ServerAliveInterval=60",  # keep a quiet connection from dropping
        cfg.server_ssh,
    ]


def cmd_tunnel(args, cfg, conn):
    if not cfg.server_ssh:
        sys.exit(
            "Set the server in config.toml first, e.g.:\n"
            "  [server]\n"
            '  ssh = "root@your-server.example.com"'
        )
    conn.close()
    print(
        f"Connecting to {cfg.server_ssh}...\n"
        f"While this runs, the server's web UI is at http://127.0.0.1:{cfg.tunnel_port}\n"
        "Press Ctrl-C to close the tunnel."
    )
    try:
        result = subprocess.run(tunnel_command(cfg))
    except KeyboardInterrupt:
        print("\nTunnel closed.")
        return
    if result.returncode != 0:
        sys.exit(f"The tunnel stopped (ssh exit code {result.returncode}).")


def cmd_test_notify(args, cfg, conn):
    try:
        send_test(cfg)
    except NotifyError as e:
        sys.exit(str(e))
    print("Sent a test notification. Check your phone.")


def format_item(item: WatchItem) -> str:
    flags = [
        "sale only" if item.sale_only else "any post",
        "fuzzy" if item.fuzzy else "no fuzzy",
    ]
    lines = [f"  #{item.id}  {item.name}  [{', '.join(flags)}]"]
    if item.variants:
        lines.append(f"       variants: {', '.join(item.variants)}")
    if item.excludes:
        lines.append(f"       excludes: {', '.join(item.excludes)}")
    if item.notes:
        lines.append(f"       notes: {item.notes}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reddit-notifier",
        description="Get a phone notification when a Reddit post mentions something on your watchlist.",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_PATH, help="settings file (default: config.toml)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("poll", help="check the feed once")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="preview: show every recent post (~12 hours) that matches your watchlist;"
        " don't notify or save anything",
    )
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("run", help="keep checking the feed until stopped (Ctrl-C)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="open the web UI (http://127.0.0.1:5050 by default)")
    p.add_argument("--port", type=int, help="port to use instead of web_port in config.toml")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser(
        "service",
        help="run the poller and web UI in the background on this Mac",
        description="Run the poller and web UI in the background (macOS launchd)."
        " start: start now and at every login (also restarts them)."
        " stop: stop, and don't start again until `start`."
        " status: are they running, and when was the last poll."
        " logs: show recent log lines.",
    )
    p.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    p.add_argument("-n", "--lines", type=int, default=20, help="with logs: lines per log")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("tunnel", help="show the server's web UI on this computer (via ssh)")
    p.set_defaults(func=cmd_tunnel)

    p = sub.add_parser("test-notify", help="send a test notification to your phone")
    p.set_defaults(func=cmd_test_notify)

    watch = sub.add_parser("watch", help="manage the watchlist").add_subparsers(
        dest="watch_command", required=True
    )
    p = watch.add_parser("add", help="add a fragrance to watch for")
    p.add_argument("name", help='e.g. "Viride" (use quotes if it has spaces)')
    p.add_argument("--variant", action="append", default=[], help="another spelling to match; repeatable")
    p.add_argument("--exclude", action="append", default=[], help="phrase to ignore, e.g. a flanker; repeatable")
    p.add_argument("--sale-only", action="store_true", help="only match [WTS] posts")
    p.add_argument("--no-fuzzy", action="store_true", help="turn off typo-tolerant matching")
    p.add_argument("--notes", default="", help="free-text notes for yourself")
    p.set_defaults(func=cmd_watch_add)

    p = watch.add_parser("list", help="show the watchlist")
    p.set_defaults(func=cmd_watch_list)

    p = watch.add_parser("remove", help="remove a watch item by its number")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_watch_remove)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        sys.exit(str(e))
    conn = db.connect(cfg.db_path)
    args.func(args, cfg, conn)
