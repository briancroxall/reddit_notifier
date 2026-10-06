"""Fetch a subreddit's newest posts from its public RSS (Atom) feed.

This is the only module that knows where posts come from. If Reddit API
access is approved later, a PRAW version of fetch_posts() can replace this
without changing the rest of the program.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

ATOM = {"a": "http://www.w3.org/2005/Atom"}
TIMEOUT_SECONDS = 30


@dataclass
class Post:
    id: str  # e.g. "t3_1wwtddy"
    title: str
    body_text: str
    url: str
    author: str
    published: str  # ISO-8601 string from the feed


class FetchError(Exception):
    """The feed couldn't be fetched or read (network problem, 403, bad XML...)."""


class RateLimited(FetchError):
    """Reddit answered 429 Too Many Requests."""

    def __init__(self, retry_after: float | None):
        self.retry_after = retry_after
        wait = f"; retry after {retry_after:.0f}s" if retry_after else ""
        super().__init__(f"Rate limited by Reddit (HTTP 429){wait}")


class _PostBodyText(HTMLParser):
    """Collect the text inside <div class="md">, which is the post itself.

    Reddit's feed puts a footer after it ("submitted by /u/name [link]
    [comments]"); skipping that keeps usernames and URLs from matching.
    """

    def __init__(self):
        super().__init__()
        self.depth = 0  # >0 while inside the md div
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            if tag == "div":
                self.depth += 1
            self.parts.append(" ")  # keep words in separate tags apart
        elif tag == "div" and "md" in (dict(attrs).get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            if tag == "div":
                self.depth -= 1
            self.parts.append(" ")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def html_to_text(content_html: str) -> str:
    parser = _PostBodyText()
    parser.feed(content_html)
    return " ".join("".join(parser.parts).split())


def parse_feed(xml_text: str) -> list[Post]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise FetchError(f"Couldn't read the feed's XML: {e}") from e

    posts = []
    for entry in root.findall("a:entry", ATOM):
        link = entry.find("a:link", ATOM)
        posts.append(
            Post(
                id=entry.findtext("a:id", "", ATOM),
                title=entry.findtext("a:title", "", ATOM),
                body_text=html_to_text(entry.findtext("a:content", "", ATOM)),
                url=link.get("href", "") if link is not None else "",
                author=entry.findtext("a:author/a:name", "", ATOM),
                published=entry.findtext("a:published", "", ATOM),
            )
        )
    return posts


def _retry_after(response: requests.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def fetch_posts(feed_url: str, user_agent: str) -> list[Post]:
    """Download and parse the feed. Raises RateLimited or FetchError."""
    try:
        response = requests.get(
            feed_url, headers={"User-Agent": user_agent}, timeout=TIMEOUT_SECONDS
        )
    except requests.RequestException as e:
        raise FetchError(f"Network problem: {e}") from e

    if response.status_code == 429:
        raise RateLimited(_retry_after(response))
    if response.status_code == 403:
        raise FetchError(
            "Reddit refused the request (HTTP 403). It may be blocking this"
            " network or the feed may no longer be public."
        )
    if response.status_code != 200:
        raise FetchError(f"Unexpected HTTP {response.status_code} from Reddit")
    return parse_feed(response.text)
