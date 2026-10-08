"""Load settings from config.toml."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PATH = Path("config.toml")
MIN_INTERVAL_MINUTES = 2


class ConfigError(Exception):
    pass


@dataclass
class Config:
    subreddit: str
    poll_interval_minutes: float
    user_agent: str
    db_path: Path
    ntfy_server: str
    ntfy_topic: str
    web_port: int = 5050
    # Send a phone alert once polling has failed for this long.
    alert_after_minutes: float = 60
    # Normally built from `subreddit`; set it to test against another address.
    custom_feed_url: str = ""
    # [server] section: where the notifier runs remotely, for `tunnel`.
    server_ssh: str = ""  # e.g. "root@example.com"
    tunnel_port: int = 5051  # local port for the tunneled web UI
    # Time zone for times shown in the web UI and logs, e.g. "America/Chicago".
    # Empty means this computer's own time zone.
    timezone: str = ""
    # How many already-read posts to keep on the home page, below the unread
    # ones. The rest are on the "Past matches" page.
    read_matches_on_home: int = 5

    @property
    def feed_url(self) -> str:
        if self.custom_feed_url:
            return self.custom_feed_url
        return f"https://www.reddit.com/r/{self.subreddit}/new/.rss?limit=100"


def load_config(path: Path = DEFAULT_PATH) -> Config:
    if not path.exists():
        raise ConfigError(
            f"No {path} found. Create one with:\n"
            "  cp config.example.toml config.toml"
        )
    with path.open("rb") as f:
        data = tomllib.load(f)

    ntfy = data.get("ntfy", {})
    topic = ntfy.get("topic", "")
    if not topic or topic == "CHANGE-ME":
        raise ConfigError(f"Set [ntfy] topic in {path}.")

    timezone = data.get("timezone", "")
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(
                f"Unknown timezone {timezone!r} in {path}."
                ' Use a name like "America/Chicago" or "America/Denver".'
            ) from None

    interval = float(data.get("poll_interval_minutes", 10))
    # A relative db_path is relative to this config file, not to wherever the
    # command runs from. Background services don't start in the project folder.
    db_path = path.resolve().parent / data.get("db_path", "data/notifier.db")
    return Config(
        subreddit=data.get("subreddit", "fragranceswap"),
        poll_interval_minutes=max(interval, MIN_INTERVAL_MINUTES),
        user_agent=data.get(
            "user_agent", "python:reddit-notifier:v0.1 (personal notifier)"
        ),
        db_path=db_path,
        ntfy_server=ntfy.get("server", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=topic,
        web_port=int(data.get("web_port", 5050)),
        alert_after_minutes=float(data.get("alert_after_minutes", 60)),
        custom_feed_url=data.get("feed_url", ""),
        server_ssh=data.get("server", {}).get("ssh", ""),
        tunnel_port=int(data.get("server", {}).get("tunnel_port", 5051)),
        timezone=timezone,
        read_matches_on_home=max(int(data.get("read_matches_on_home", 5)), 0),
    )
