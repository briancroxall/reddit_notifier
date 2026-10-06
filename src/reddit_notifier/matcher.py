"""Decide whether a post mentions a fragrance on the watchlist.

Matching happens in two passes:

1. Exact: after normalizing (lowercase, no accents, punctuation -> spaces),
   look for each name/variant as whole words. "Pêche" matches "peche",
   "Vi-ride" matches a "Vi ride" variant, but "Viride" won't match inside
   "Virideum".
2. Fuzzy (if the item allows it): compare runs of words against each term,
   ignoring spaces and allowing a small number of typos. Catches "Virdie"
   (swapped letters) and "Vi ride" (split word) without listing them.
"""

import html
import re
import unicodedata
from dataclasses import dataclass, field

# Terms shorter than this never fuzzy-match: too many real words are one
# typo away from a short name (e.g. "Orage" vs "orange").
FUZZY_MIN_LENGTH = 6
# Terms this long or longer may be two typos off; shorter ones only one.
FUZZY_TWO_TYPOS_LENGTH = 10

SALE_TAG = re.compile(r"\[[^\]]*\bwts\b[^\]]*\]", re.IGNORECASE)


@dataclass
class WatchItem:
    name: str
    variants: list[str] = field(default_factory=list)
    sale_only: bool = False
    fuzzy: bool = True
    notes: str = ""
    id: int | None = None

    @property
    def terms(self) -> list[str]:
        return [self.name, *self.variants]


@dataclass
class Match:
    item: WatchItem
    term: str  # the watchlist name or variant that matched
    matched_text: str  # what the post actually said (normalized)
    fuzzy: bool
    where: str  # "title" or "body"


def normalize(text: str) -> str:
    """Lowercase, strip accents, and reduce everything else to single spaces."""
    text = html.unescape(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return text.strip()


def is_sale_post(title: str) -> bool:
    """True if the title has a [WTS] tag (also [WTS/WTT], [wts], etc.)."""
    return bool(SALE_TAG.search(title))


def typo_distance(a: str, b: str) -> int:
    """Number of single-letter edits (insert, delete, replace, or swap two
    neighbors) needed to turn a into b. "virdie" -> "viride" is 1."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(
                rows[i - 1][j] + 1,  # delete
                row[j - 1] + 1,  # insert
                rows[i - 1][j - 1] + cost,  # replace
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[i - 2][j - 2] + 1)  # swap
        rows.append(row)
    return rows[-1][-1]


def allowed_typos(term: str) -> int:
    length = len(term.replace(" ", ""))
    if length < FUZZY_MIN_LENGTH:
        return 0
    if length < FUZZY_TWO_TYPOS_LENGTH:
        return 1
    return 2


def find_exact(term: str, text: str) -> str | None:
    if term and f" {term} " in f" {text} ":
        return term
    return None


def find_fuzzy(term: str, text: str) -> str | None:
    """Slide over the text in runs of words around the term's word count,
    and compare each run (spaces removed) to the term (spaces removed)."""
    max_typos = allowed_typos(term)
    if max_typos == 0:
        return None
    target = term.replace(" ", "")
    words = text.split()
    n = len(term.split())
    for width in (n - 1, n, n + 1):
        if width < 1:
            continue
        for i in range(len(words) - width + 1):
            chunk = words[i : i + width]
            candidate = "".join(chunk)
            if abs(len(candidate) - len(target)) > max_typos:
                continue
            if typo_distance(candidate, target) <= max_typos:
                return " ".join(chunk)
    return None


def match_item(item: WatchItem, title: str, body: str) -> Match | None:
    """Best match for one watch item: exact beats fuzzy, title beats body."""
    if item.sale_only and not is_sale_post(title):
        return None
    fields = [("title", normalize(title)), ("body", normalize(body))]
    terms = [(t, normalize(t)) for t in item.terms]

    for where, text in fields:
        for raw, term in terms:
            if found := find_exact(term, text):
                return Match(item, raw, found, fuzzy=False, where=where)

    if item.fuzzy:
        for where, text in fields:
            for raw, term in terms:
                if found := find_fuzzy(term, text):
                    return Match(item, raw, found, fuzzy=True, where=where)
    return None


def match_post(post, items: list[WatchItem]) -> list[Match]:
    """All watch items that a post matches. `post` needs .title and .body_text."""
    matches = []
    for item in items:
        if m := match_item(item, post.title, post.body_text):
            matches.append(m)
    return matches
