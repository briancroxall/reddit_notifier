"""Command-line interface: `uv run reddit-notifier --help`."""

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import db
from .config import DEFAULT_PATH, Config, ConfigError, load_config
from .feed import FetchError, RateLimited, fetch_posts
from .matcher import WatchItem, match_post
from .notify import NotifyError, describe, notify_post, send_test

MAX_WAIT_SECONDS = 30 * 60


def say(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# --- polling -----------------------------------------------------------------


def poll_once(cfg: Config, conn, dry_run: bool = False, dry_seen: set[str] | None = None) -> None:
    """Fetch the feed once, then notify about (or print) new matching posts.

    A real poll on an empty database only records what's already there, so
    you aren't flooded with alerts for old posts. A dry run never writes to
    the database; `dry_seen` stops `run --dry-run` repeating itself.
    Raises FetchError / RateLimited if the feed can't be fetched.
    """
    posts = fetch_posts(cfg.feed_url, cfg.user_agent)
    ids = [p.id for p in posts]

    if not dry_run and not db.has_seen_any(conn):
        db.mark_seen(conn, ids)
        db.log_poll(conn, "seeded", n_posts=len(posts))
        say(f"First run: recorded {len(posts)} existing posts. Only newer posts will alert you.")
        return

    new_ids = db.unseen(conn, ids)
    if dry_seen is not None:
        new_ids -= dry_seen
        dry_seen |= new_ids
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
            if dry_run:
                say(f"WOULD NOTIFY: {post.title}\n    {summary}\n    {post.url}")
            else:
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

    if not dry_run:
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


def run_forever(cfg: Config, conn, dry_run: bool) -> None:
    say(
        f"Watching r/{cfg.subreddit} every {cfg.poll_interval_minutes:g} minutes"
        f"{' (dry run)' if dry_run else ''}. Press Ctrl-C to stop."
    )
    dry_seen: set[str] | None = set() if dry_run else None
    failures = 0
    while True:
        retry_after = None
        try:
            poll_once(cfg, conn, dry_run, dry_seen)
            failures = 0
        except RateLimited as e:
            failures += 1
            retry_after = e.retry_after
            say(str(e))
            if not dry_run:
                db.log_poll(conn, "error", error=str(e))
        except FetchError as e:
            failures += 1
            say(f"Couldn't fetch the feed: {e}")
            if not dry_run:
                db.log_poll(conn, "error", error=str(e))
        except Exception:
            # A bug shouldn't stop the watcher for good; log it and keep going.
            failures += 1
            say("Unexpected error:\n" + traceback.format_exc())
            if not dry_run:
                db.log_poll(conn, "error", error=traceback.format_exc(limit=3))

        wait = next_wait(cfg, failures, retry_after)
        if failures:
            say(f"Trying again in {wait / 60:.0f} minutes.")
        time.sleep(wait)


# --- commands ----------------------------------------------------------------


def cmd_poll(args, cfg, conn):
    try:
        poll_once(cfg, conn, dry_run=args.dry_run)
    except FetchError as e:
        if not args.dry_run:
            db.log_poll(conn, "error", error=str(e))
        sys.exit(f"Couldn't fetch the feed: {e}")


def cmd_run(args, cfg, conn):
    try:
        run_forever(cfg, conn, dry_run=args.dry_run)
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
    p.add_argument("--dry-run", action="store_true", help="print matches; don't notify or save anything")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("run", help="keep checking the feed until stopped (Ctrl-C)")
    p.add_argument("--dry-run", action="store_true", help="print matches; don't notify or save anything")
    p.set_defaults(func=cmd_run)

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
