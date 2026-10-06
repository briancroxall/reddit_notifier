"""Send push notifications through ntfy (https://ntfy.sh).

Messages are published as JSON rather than with HTTP headers, because headers
can garble non-ASCII text like "Virēre" or emoji in post titles.

Send yourself a test notification with:
    uv run python -m reddit_notifier.notify
"""

import requests

from .config import Config, load_config
from .matcher import Match

TIMEOUT_SECONDS = 15


class NotifyError(Exception):
    pass


def send(
    cfg: Config,
    title: str,
    message: str,
    click: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Publish one notification to the configured topic. Raises NotifyError."""
    payload = {"topic": cfg.ntfy_topic, "title": title, "message": message}
    if click:
        payload["click"] = click  # tapping the notification opens this URL
    if tags:
        payload["tags"] = tags  # emoji shortcodes, e.g. "perfume"
    try:
        response = requests.post(cfg.ntfy_server, json=payload, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise NotifyError(f"Couldn't reach ntfy: {e}") from e
    if response.status_code != 200:
        raise NotifyError(f"ntfy returned HTTP {response.status_code}: {response.text[:200]}")


def describe(match: Match) -> str:
    """One line per match, e.g. 'Viride (fuzzy: "virdie", in body)'."""
    details = []
    if match.fuzzy:
        details.append(f'fuzzy: "{match.matched_text}"')
    elif match.term != match.item.name:
        details.append(f'as "{match.term}"')
    if match.where == "body":
        details.append("in body")
    return f"{match.item.name} ({', '.join(details)})" if details else match.item.name


def notify_post(cfg: Config, post, matches: list[Match]) -> None:
    """One notification per post, listing every watch item it matched.
    `post` needs .title and .url."""
    lines = [f"Matched: {describe(m)}" for m in matches]
    send(cfg, title=post.title, message="\n".join(lines), click=post.url, tags=["perfume"])


def send_test(cfg: Config) -> None:
    send(
        cfg,
        title="reddit-notifier test",
        message=f"Notifications for r/{cfg.subreddit} are working. Tap to open the subreddit.",
        click=f"https://www.reddit.com/r/{cfg.subreddit}/new/",
        tags=["perfume", "white_check_mark"],
    )


if __name__ == "__main__":
    config = load_config()
    send_test(config)
    print(f"Sent a test notification to topic {config.ntfy_topic!r}.")
