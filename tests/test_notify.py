"""Notification tests use a fake requests.post, so nothing reaches your phone."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from reddit_notifier import notify
from reddit_notifier.config import Config
from reddit_notifier.matcher import Match, WatchItem

CFG = Config(
    subreddit="fragranceswap",
    poll_interval_minutes=5,
    user_agent="ua",
    db_path=Path("unused.db"),
    ntfy_server="https://ntfy.example",
    ntfy_topic="test-topic",
)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been posted to ntfy."""
    calls = []

    def post(url, json, timeout):
        calls.append({"url": url, "json": json})
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", post)
    return calls


def match(name, term=None, text=None, fuzzy=False, where="title"):
    term = term or name
    return Match(WatchItem(name), term, text or term.lower(), fuzzy, where)


def test_notify_post_payload(sent):
    post = SimpleNamespace(title="[WTS] Virēre & Viride 😜", url="https://reddit.com/x")
    notify.notify_post(CFG, post, [match("Viride"), match("Virēre", where="body")])

    [call] = sent
    assert call["url"] == "https://ntfy.example"
    payload = call["json"]
    assert payload["topic"] == "test-topic"
    assert payload["title"] == "[WTS] Virēre & Viride 😜"  # non-ASCII intact
    assert payload["click"] == "https://reddit.com/x"
    assert payload["message"] == "Matched: Viride\nMatched: Virēre (in body)"


def test_describe():
    assert notify.describe(match("Viride")) == "Viride"
    assert notify.describe(match("Viride", term="Vi ride")) == 'Viride (as "Vi ride")'
    assert (
        notify.describe(match("Viride", text="virdie", fuzzy=True, where="body"))
        == 'Viride (fuzzy: "virdie", in body)'
    )


def test_send_test(sent):
    notify.send_test(CFG)
    assert sent[0]["json"]["click"] == "https://www.reddit.com/r/fragranceswap/new/"


def test_http_error(monkeypatch):
    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: FakeResponse(429, "slow down"))
    with pytest.raises(notify.NotifyError, match="429"):
        notify.send_test(CFG)


def test_network_error(monkeypatch):
    def post(url, json, timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(notify.requests, "post", post)
    with pytest.raises(notify.NotifyError, match="Couldn't reach ntfy"):
        notify.send_test(CFG)
