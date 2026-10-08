# Running reddit-notifier on a server (DigitalOcean droplet)

This runbook moves the notifier from a Mac to an always-on Ubuntu server, in
this case the droplet that already runs Noted. Afterward:

- The **poller** runs around the clock as a systemd service and sends phone
  alerts as before.
- The **web UI** runs on the server too, but only listens on the server's
  own `127.0.0.1`, so it can't be reached from the internet. You view it
  from your Mac through an SSH tunnel (`reddit-notifier tunnel`).
- **No firewall, nginx, or DNS changes** are needed.

It assumes the setup from Noted's runbook: Ubuntu 24.04, a `fragdash` user
with uv at `/home/fragdash/.local/bin/uv`, and SSH access as root. It also
works on a Raspberry Pi running Raspberry Pi OS (Debian) with a user of your
choice; change `fragdash` in the commands and in the `deploy/*.service` files.

**Important:** only one copy of the poller should run at a time, either the
Mac or the server. Two would double-check Reddit and could send duplicate
alerts. The switch-over below stops the Mac first.

## 0. Check that Reddit allows the server

From the server, one request with the notifier's User-Agent:

```sh
U='https://www.reddit.com/r/fragranceswap/new/.rss'
A='python:reddit-notifier:v0.1 (by /u/yourname)'
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' -A "$A" "$U"
```

`200 application/atom+xml` means it's allowed. A `403` means Reddit blocks
this server's network, and the notifier should stay on a home computer.

## 1. On your Mac: tell things where the server is

Two separate things, both on the Mac.

**A. A temporary shortcut for this terminal window.** Type this, using
whatever you normally type after `ssh`:

```sh
SERVER=root@your-server.example.com
```

It prints nothing. It creates a shell variable: later commands say `$SERVER`
and the shell swaps in the address, which keeps them short. It only lasts
until you close that terminal window, so type it again in any new window you
use for this runbook. Check it with `echo $SERVER`.

**B. A permanent setting in `config.toml`,** so `reddit-notifier tunnel`
(step 6) knows where to connect. Add these lines at the **end** of the file
(in TOML, everything after a `[section]` line belongs to that section):

```toml
[server]
ssh = "root@your-server.example.com"
```

## 2. On the server: install the code

Log in:

```sh
ssh $SERVER
```

Note the memory in use before adding anything:

```sh
free -h
```

Create the app folder for the `fragdash` user:

```sh
mkdir -p /opt/reddit-notifier
chown fragdash:fragdash /opt/reddit-notifier
```

Switch to the `fragdash` user (the prompt changes), then fetch the code and
install its dependencies. uv downloads Python 3.14 itself if needed.

```sh
su - fragdash
.local/bin/uv self update
git clone https://github.com/briancroxall/reddit_notifier.git /opt/reddit-notifier
cd /opt/reddit-notifier
~/.local/bin/uv sync --frozen
mkdir -p data
exit
```

`exit` returns you to the root prompt. Stay logged in.

## 3. On your Mac: send the settings

In a **second** Mac terminal, from the project folder:

```sh
cd ~/Documents/github/reddit_notifier
scp config.toml $SERVER:/opt/reddit-notifier/
```

(Set `SERVER=...` again in this terminal first if it's a new window.)

Back on the server, as root, make the file belong to `fragdash` and keep it
private, since it holds your ntfy topic:

```sh
chown fragdash:fragdash /opt/reddit-notifier/config.toml
chmod 600 /opt/reddit-notifier/config.toml
```

Servers often run on UTC, which would make the web UI show UTC times. If
`timedatectl` on the server doesn't show your time zone, open
`/opt/reddit-notifier/config.toml` and set `timezone` (above the `[ntfy]`
line) to yours, e.g. `timezone = "America/Chicago"`.

## 4. Switch over: stop the Mac, move the database, start the server

Do these in order.

**On the Mac**, stop the background service so the database stops changing:

```sh
uv run reddit-notifier service stop
```

**On the Mac**, copy the database (watchlist, seen posts, match history):

```sh
scp data/notifier.db $SERVER:/opt/reddit-notifier/data/
```

**On the server**, as root:

```sh
chown fragdash:fragdash /opt/reddit-notifier/data/notifier.db
cd /opt/reddit-notifier
cp deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now reddit-notifier reddit-notifier-web
```

`enable --now` starts both services now and at every boot.

## 5. Check that it's working

On the server:

```sh
systemctl status reddit-notifier reddit-notifier-web
```

Both should say `active (running)`. Press `q` to leave the status view.
Then watch the poller's log:

```sh
journalctl -u reddit-notifier -f
```

It shows the latest lines, including `Watching r/fragranceswap every 10
minutes` and `Checked 100 posts: ...`, then waits for new ones. The poller
only writes about one line per poll, so after that it can look idle for up to
10 minutes; that's normal. Press Ctrl-C to stop watching (the service keeps
running). To print recent lines and exit instead:
`journalctl -u reddit-notifier -n 20 --no-pager`.

Check memory again and compare it with step 2:

```sh
free -h
```

## 6. See the web UI from your Mac

On the Mac:

```sh
uv run reddit-notifier tunnel
```

While it runs, open <http://127.0.0.1:5051>. That's the server's web UI,
with your history copied from the Mac. Press Ctrl-C to close the tunnel.
(Port 5051 keeps it separate from 5050, which the Mac's own service uses if
you ever run it.)

## Day to day

| Task | Where | Command |
|---|---|---|
| See the web UI | Mac | `uv run reddit-notifier tunnel`, then <http://127.0.0.1:5051> |
| Is it running? | server | `systemctl status reddit-notifier` |
| Recent log lines | server | `journalctl -u reddit-notifier -n 30` |
| Stop both | server | `systemctl stop reddit-notifier reddit-notifier-web` |
| Start both | server | `systemctl start reddit-notifier reddit-notifier-web` |
| Update to the latest code | server | `bash /opt/reddit-notifier/deploy/update.sh` |
| Change settings | server | edit `/opt/reddit-notifier/config.toml`, then run the update script (it restarts both) |

Watchlist changes made in the web UI take effect at the next poll; no
restart needed.

To update: commit and push from the Mac first, then run the update script on
the server. It pulls from GitHub, syncs dependencies, refreshes the service
files, and restarts both services.

## Moving back to the Mac

If Reddit starts refusing the server (the phone's "having trouble" alert
will say `HTTP 403`), or you just want to move back:

On the server:

```sh
systemctl disable --now reddit-notifier reddit-notifier-web
```

On the Mac, from the project folder:

```sh
scp $SERVER:/opt/reddit-notifier/data/notifier.db data/
uv run reddit-notifier service start
```

## Troubleshooting

- **`status=203/EXEC` in `systemctl status`:** systemd can't find the
  program. The project's environment is probably missing: run
  `bash /opt/reddit-notifier/deploy/update.sh`, which runs `uv sync`.
- **Service keeps restarting:** read the error with
  `journalctl -u reddit-notifier -n 50`.
- **`tunnel` says the port is in use:** another tunnel is already open, or
  change `tunnel_port` under `[server]` in the Mac's `config.toml`.
- **Times in the web UI look off:** they use the server's time zone
  (`timedatectl` shows it).
