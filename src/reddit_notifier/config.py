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

    @property
    def feed_url(self) -> str:
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
    return Config(
        subreddit=data.get("subreddit", "fragranceswap"),
        poll_interval_minutes=max(interval, MIN_INTERVAL_MINUTES),
        user_agent=data.get(
            "user_agent", "python:reddit-notifier:v0.1 (personal notifier)"
        ),
        db_path=Path(data.get("db_path", "data/notifier.db")),
        ntfy_server=ntfy.get("server", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=topic,
        web_port=int(data.get("web_port", 5050)),
    )
