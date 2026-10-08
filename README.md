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
- **Tells you when it's in trouble.** If checks keep failing for an hour
  (no internet, Reddit refusing), you get one alert, and another when it's
  working again.
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

To try it out, start the watcher in a terminal (see
[Running it in the background](#running-it-in-the-background) for the
long-term options):

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

## Running it in the background

Run **one** of these at a time, never both: two watchers would check Reddit
twice and could send duplicate alerts.

### On a Mac, while it's awake

```sh
uv run reddit-notifier service start
```

This runs the watcher and web UI in the background with macOS's launchd. They
start again at every login and restart if they crash, with no terminal
needed. They pause while the Mac sleeps and catch up when it wakes.

| Command | What it does |
|---|---|
| `service start` | Start both now and at every login (also restarts them, e.g. after a code change) |
| `service stop` | Stop both; they stay off, even after a reboot, until `start` |
| `service status` | Are they running, and when was the last check? |
| `service logs` | Recent log lines (`-n 50` for more) |

Logs are in `~/Library/Logs/reddit-notifier/`.

### On a server, around the clock

[docs/deploy-droplet.md](docs/deploy-droplet.md) is a step-by-step runbook
for an Ubuntu server, such as a small DigitalOcean droplet. It also works on
a Raspberry Pi. The watcher and web UI run as systemd services, and the web
UI stays private to the server: from your own computer, run

```sh
uv run reddit-notifier tunnel
```

and open <http://127.0.0.1:5051> while it runs. It connects over SSH to the
server named in the `[server]` section of `config.toml`.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `subreddit` | `fragranceswap` | Subreddit to watch (no `r/`) |
| `poll_interval_minutes` | `10` | Minutes between checks (minimum 2) |
| `user_agent` | | Identifies the tool to Reddit; include your username |
| `db_path` | `data/notifier.db` | SQLite database location |
| `web_port` | `5050` | Port for the web UI |
| `alert_after_minutes` | `60` | Send a "having trouble" alert after checks fail this long |
| `timezone` | this computer's | Time zone for times in the web UI and logs, e.g. `America/Denver`; set it on servers, which often use UTC |
| `feed_url` | built from `subreddit` | Override the feed address (mainly for testing) |
| `[ntfy] server` | `https://ntfy.sh` | ntfy server |
| `[ntfy] topic` | | Your private topic name |
| `[server] ssh` | | Where the notifier runs remotely, as you'd type it after `ssh` (for `tunnel`) |
| `[server] tunnel_port` | `5051` | Local port for the tunneled web UI |

`config.toml` holds your ntfy topic, which works like a password. Keep it out
of git, along with editor backup files such as vim's `.config.toml.swp`
(both are in `.gitignore`).

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
| `src/reddit_notifier/cli.py` | Command-line interface, polling loop, trouble alerts |
| `src/reddit_notifier/service.py` | macOS background service (launchd) |
| `src/reddit_notifier/web/` | Flask web UI |
| `deploy/` | systemd services and update script for a Linux server |
| `docs/deploy-droplet.md` | Server setup runbook |

## Limitations

- **Cloud servers may get blocked.** Reddit reportedly blocks many
  datacenter IP addresses. A DigitalOcean droplet was allowed when tested in
  October 2026, but that could change. If it does, the "having trouble"
  alert will show `HTTP 403`, and the runbook explains how to move back to a
  home computer.
- **On a Mac, it pauses while the Mac sleeps.** It catches up on wake, as
  long as fewer than ~100 posts (about 12 hours) went by.
- **Reddit could close the RSS feed**, as it did the `.json` endpoints. If
  that happens, `feed.py` would need to move to the official API.
- **RSS doesn't include post flair**, so "sale only" relies on the `[WTS]`
  tag in the title, which r/fragranceswap uses consistently.

## License

[MIT](LICENSE)
