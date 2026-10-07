# reddit_notifier

Get a push notification on your phone when someone posts a listing on
[r/fragranceswap](https://www.reddit.com/r/fragranceswap/) that mentions a
fragrance on your watchlist.

Hard-to-find bottles often sell within hours of being posted. Instead of
refreshing the subreddit, keep a watchlist and let this tool check new posts
every few minutes and alert you through [ntfy](https://ntfy.sh).

## What it does

- **Watches new posts** in a subreddit (r/fragranceswap by default), checking
  both titles and post bodies, since multi-bottle listings often name
  fragrances only in the body.
- **Matches flexibly.** It ignores case, accents (`Pêche` = `Peche`,
  `Virēre` = `Virere`), hyphens, and extra spaces, and it tolerates small
  typos (`Virdie` → `Viride`, `Vi ride` → `Viride`). Short names (under six
  letters) must match exactly, so `Orage` doesn't match `orange`.
- **Lets you tune each watch item** with:
  - *variants*: other spellings or abbreviations (`MDC 30`)
  - *excludes*: similarly named fragrances to skip (`Aventus Cologne` when
    watching `Aventus`). A post that mentions both still matches.
  - *sale only*: only alert on `[WTS]` (want to sell) posts
  - *typo tolerance*: on or off
- **Notifies once per post.** Each alert shows the post title and what
  matched; tapping it opens the post.
- **Has a small local web page** for managing the watchlist, reviewing
  matches (with read/unread tracking), checking recent posts for an item, and
  seeing whether the watcher is running.

## How it gets data

The tool reads the subreddit's public RSS feed (`/r/<sub>/new/.rss`), which
returns the newest 100 posts, roughly the last 12 hours on r/fragranceswap.
It sends a descriptive User-Agent and polls no more often than every 2
minutes (10 by default).

It does not use Reddit's official API: since late 2025 that requires
pre-approval, and Reddit's anonymous `.json` endpoints were shut off in
May 2026. All feed access lives in `feed.py`, so switching to the official
API later would only mean changing that one file.

Reddit rate-limits the feed. When it answers `429 Too Many Requests`, the
watcher backs off (10, 20, then up to 30 minutes) and tries again.

## Requirements

- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- The free ntfy app on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625396347),
  [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))

## Setup

```sh
git clone https://github.com/briancroxall/reddit_notifier.git
cd reddit_notifier
uv sync
cp config.example.toml config.toml
```

Then edit `config.toml`:

1. Pick a hard-to-guess ntfy topic (anyone who knows the name can read it)
   and put it in `[ntfy] topic`. To generate one:
   ```sh
   python3 -c "import secrets; print('fragswap-' + secrets.token_hex(6))"
   ```
2. Put your Reddit username in `user_agent`.
3. In the ntfy app, subscribe to the same topic on the default server.

Check that notifications reach your phone:

```sh
uv run reddit-notifier test-notify
```

`config.toml` and the `data/` folder (the SQLite database) are gitignored.

## Usage

Add fragrances to your watchlist, either in the web UI (below) or from the
command line:

```sh
uv run reddit-notifier watch add Viride --sale-only
uv run reddit-notifier watch add "Mousse de Chene 30" --variant "MDC 30"
uv run reddit-notifier watch add Aventus --exclude "Aventus Cologne"
uv run reddit-notifier watch list
uv run reddit-notifier watch remove 3
```

Start the watcher and leave it running in a terminal:

```sh
uv run reddit-notifier run
```

The first poll records the posts already in the feed without notifying you,
so you only hear about posts made after you start. Stop it with Ctrl-C.

Preview what your watchlist matches in the recent feed, without sending
notifications or saving anything:

```sh
uv run reddit-notifier poll --dry-run
```

### Web UI

In a second terminal:

```sh
uv run reddit-notifier web
```

Then open <http://127.0.0.1:5050>. The web UI and the watcher are separate
programs that share one database: alerts keep coming whether or not the page
is open, and watchlist changes take effect at the watcher's next poll.

- **Matches:** recent matches, newest first, with unread ones marked. The
  page also shows when the watcher last checked Reddit and warns if it seems
  to have stopped.
- **Watchlist:** add, edit, and delete items.
- **Check recent posts:** run the watchlist (or one item) against the last
  ~12 hours of posts. This is useful right after adding something, since new
  items only apply to posts that appear afterward.

The web UI only listens on your own computer and has no login. Don't expose
it to a network.

Run `uv run reddit-notifier --help` for all commands and options.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `subreddit` | `fragranceswap` | Subreddit to watch (no `r/`) |
| `poll_interval_minutes` | `10` | Minutes between checks (minimum 2) |
| `user_agent` | | Identifies the tool to Reddit; include your username |
| `db_path` | `data/notifier.db` | SQLite database location |
| `web_port` | `5050` | Port for the web UI |
| `[ntfy] server` | `https://ntfy.sh` | ntfy server |
| `[ntfy] topic` | | Your private topic name |

## Development

```sh
uv run pytest
```

The tests use fake feeds and a fake notifier, so they never contact Reddit or
send notifications.

| File | Purpose |
|---|---|
| `src/reddit_notifier/feed.py` | Fetches and parses the RSS feed |
| `src/reddit_notifier/matcher.py` | Normalizing, exact and fuzzy matching, excludes, sale filter |
| `src/reddit_notifier/db.py` | SQLite: watchlist, seen posts, matches, poll log |
| `src/reddit_notifier/notify.py` | ntfy notifications |
| `src/reddit_notifier/cli.py` | Command-line interface and polling loop |
| `src/reddit_notifier/web/` | Flask web UI |

## Limitations

- **The watcher only runs while your computer is awake** and the terminal is
  open. Running it in the background is planned.
- **Running on a cloud server may not work.** Reddit reportedly blocks many
  datacenter IP addresses, so the tool is designed to run from home.
- **Reddit could close the RSS feed**, as it did the `.json` endpoints. If
  that happens, `feed.py` would need to move to the official API.
- **RSS doesn't include post flair**, so "sale only" relies on the `[WTS]`
  tag in the title, which r/fragranceswap uses consistently.

## License

[MIT](LICENSE)
