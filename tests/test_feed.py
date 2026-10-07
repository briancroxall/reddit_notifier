"""Feed tests use a small made-up feed shaped like Reddit's, so they never
touch the network."""

from html import escape

import pytest
import requests

from reddit_notifier import feed

BODY_HTML = (
    '<!-- SC_OFF --><div class="md"><p>Viride 100ml - $250</p>'
    "<p>Also <strong>Virēre</strong><br/>decant</p>"
    '<div class="nested"><p>Nested text</p></div></div><!-- SC_ON -->'
    ' &#32; submitted by &#32; <a href="https://www.reddit.com/user/viride_fan">'
    " /u/viride_fan </a> <span><a href=\"https://www.reddit.com/r/x/comments/1/\">[link]</a></span>"
)

FEED_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/viride_fan</name></author>
    <content type="html">{escape(BODY_HTML)}</content>
    <id>t3_abc123</id>
    <link href="https://www.reddit.com/r/fragranceswap/comments/abc123/wts_viride/" />
    <published>2026-10-06T20:00:00+00:00</published>
    <title>[WTS] Viride &amp; friends</title>
  </entry>
  <entry>
    <id>t3_def456</id>
    <link href="https://www.reddit.com/r/fragranceswap/comments/def456/" />
    <title>[WTB] Image-only post</title>
  </entry>
</feed>
"""


def test_parse_feed_fields():
    first, second = feed.parse_feed(FEED_XML)
    assert first.id == "t3_abc123"
    assert first.title == "[WTS] Viride & friends"
    assert first.url.endswith("/abc123/wts_viride/")
    assert first.author == "/u/viride_fan"
    assert first.published == "2026-10-06T20:00:00+00:00"
    # Entries with missing pieces still parse.
    assert second.id == "t3_def456"
    assert second.body_text == ""


def test_body_text_is_post_only():
    body = feed.parse_feed(FEED_XML)[0].body_text
    assert body == "Viride 100ml - $250 Also Virēre decant Nested text"
    assert "submitted by" not in body
    assert "viride_fan" not in body


def test_bad_xml():
    with pytest.raises(feed.FetchError):
        feed.parse_feed("<not really xml")


# --- fetch_posts with a fake requests.get ------------------------------------


class FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def fake_get(response=None, error=None):
    calls = []

    def get(url, headers, timeout):
        calls.append(headers)
        if error:
            raise error
        return response

    get.calls = calls
    return get


def test_fetch_ok_sends_user_agent(monkeypatch):
    get = fake_get(FakeResponse(200, FEED_XML))
    monkeypatch.setattr(feed.requests, "get", get)
    posts = feed.fetch_posts("https://example.com/feed", "my-agent")
    assert len(posts) == 2
    assert get.calls[0]["User-Agent"] == "my-agent"


def test_fetch_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(feed.requests, "get", fake_get(FakeResponse(429, headers={"Retry-After": "60"})))
    with pytest.raises(feed.RateLimited) as info:
        feed.fetch_posts("https://example.com/feed", "ua")
    assert info.value.retry_after == 60


def test_fetch_429_without_retry_after(monkeypatch):
    monkeypatch.setattr(feed.requests, "get", fake_get(FakeResponse(429)))
    with pytest.raises(feed.RateLimited) as info:
        feed.fetch_posts("https://example.com/feed", "ua")
    assert info.value.retry_after is None


@pytest.mark.parametrize("status", [403, 500, 503])
def test_fetch_http_errors(monkeypatch, status):
    monkeypatch.setattr(feed.requests, "get", fake_get(FakeResponse(status)))
    with pytest.raises(feed.FetchError) as info:
        feed.fetch_posts("https://example.com/feed", "ua")
    assert not isinstance(info.value, feed.RateLimited)


def test_fetch_network_error(monkeypatch):
    monkeypatch.setattr(feed.requests, "get", fake_get(error=requests.ConnectionError("offline")))
    with pytest.raises(feed.FetchError, match="Network problem"):
        feed.fetch_posts("https://example.com/feed", "ua")


def test_fetch_network_errors_are_short(monkeypatch):
    error = requests.ConnectionError("HTTPConnectionPool(host=...): Max retries exceeded ...")
    monkeypatch.setattr(feed.requests, "get", fake_get(error=error))
    with pytest.raises(feed.FetchError) as info:
        feed.fetch_posts("https://www.reddit.com/r/x/new/.rss", "ua")
    assert str(info.value) == "Network problem: couldn't connect to www.reddit.com"

    monkeypatch.setattr(feed.requests, "get", fake_get(error=requests.Timeout("read timed out")))
    with pytest.raises(feed.FetchError, match="no answer within 30 seconds"):
        feed.fetch_posts("https://www.reddit.com/r/x/new/.rss", "ua")
