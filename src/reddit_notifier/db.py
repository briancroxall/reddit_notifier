"""SQLite storage: the watchlist, posts already seen, matches, and a poll log.

All timestamps are stored as UTC ISO-8601 strings, e.g. "2026-10-05T17:50:45+00:00".
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .matcher import Match, WatchItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_items (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    variants    TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    excludes    TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    sale_only   INTEGER NOT NULL DEFAULT 0,
    fuzzy       INTEGER NOT NULL DEFAULT 1,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_posts (
    post_id     TEXT PRIMARY KEY,             -- Reddit id, e.g. t3_1wwtddy
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY,
    post_id       TEXT NOT NULL,
    watch_item_id INTEGER REFERENCES watch_items(id) ON DELETE SET NULL,
    item_name     TEXT NOT NULL,              -- kept even if the item is deleted
    term          TEXT NOT NULL,
    matched_text  TEXT NOT NULL,
    fuzzy         INTEGER NOT NULL,
    where_found   TEXT NOT NULL,              -- "title" or "body"
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    notified      INTEGER NOT NULL,           -- 0 for --dry-run
    matched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_log (
    id       INTEGER PRIMARY KEY,
    ran_at   TEXT NOT NULL,
    status   TEXT NOT NULL,                   -- "ok", "seeded", or "error"
    n_posts  INTEGER NOT NULL DEFAULT 0,
    n_new    INTEGER NOT NULL DEFAULT 0,
    error    TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating the file and tables if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --- watchlist ---------------------------------------------------------------


def add_watch_item(conn: sqlite3.Connection, item: WatchItem) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO watch_items (name, variants, excludes, sale_only, fuzzy, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item.name,
                json.dumps(item.variants),
                json.dumps(item.excludes),
                item.sale_only,
                item.fuzzy,
                item.notes,
                now(),
            ),
        )
    return cur.lastrowid


def list_watch_items(conn: sqlite3.Connection) -> list[WatchItem]:
    rows = conn.execute("SELECT * FROM watch_items ORDER BY name COLLATE NOCASE")
    return [
        WatchItem(
            id=row["id"],
            name=row["name"],
            variants=json.loads(row["variants"]),
            excludes=json.loads(row["excludes"]),
            sale_only=bool(row["sale_only"]),
            fuzzy=bool(row["fuzzy"]),
            notes=row["notes"],
        )
        for row in rows
    ]


def remove_watch_item(conn: sqlite3.Connection, item_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM watch_items WHERE id = ?", (item_id,))
    return cur.rowcount > 0


# --- seen posts --------------------------------------------------------------


def has_seen_any(conn: sqlite3.Connection) -> bool:
    """False only on the very first poll (used to seed without notifying)."""
    return conn.execute("SELECT 1 FROM seen_posts LIMIT 1").fetchone() is not None


def unseen(conn: sqlite3.Connection, post_ids: list[str]) -> set[str]:
    if not post_ids:
        return set()
    seen = {
        row[0]
        for row in conn.execute(
            f"SELECT post_id FROM seen_posts WHERE post_id IN ({','.join('?' * len(post_ids))})",
            post_ids,
        )
    }
    return set(post_ids) - seen


def mark_seen(conn: sqlite3.Connection, post_ids: list[str]) -> None:
    stamp = now()
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_posts (post_id, first_seen) VALUES (?, ?)",
            [(pid, stamp) for pid in post_ids],
        )


# --- matches and poll log ----------------------------------------------------


def record_match(conn: sqlite3.Connection, post, match: Match, notified: bool) -> None:
    """`post` needs .id, .title and .url."""
    with conn:
        conn.execute(
            "INSERT INTO matches (post_id, watch_item_id, item_name, term,"
            " matched_text, fuzzy, where_found, title, url, notified, matched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                post.id,
                match.item.id,
                match.item.name,
                match.term,
                match.matched_text,
                match.fuzzy,
                match.where,
                post.title,
                post.url,
                notified,
                now(),
            ),
        )


def recent_matches(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM matches ORDER BY matched_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


def log_poll(
    conn: sqlite3.Connection,
    status: str,
    n_posts: int = 0,
    n_new: int = 0,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO poll_log (ran_at, status, n_posts, n_new, error)"
            " VALUES (?, ?, ?, ?, ?)",
            (now(), status, n_posts, n_new, error),
        )


def last_poll(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM poll_log ORDER BY id DESC LIMIT 1").fetchone()
