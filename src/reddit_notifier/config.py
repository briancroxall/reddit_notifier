"""Load settings from config.toml."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

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
    )
