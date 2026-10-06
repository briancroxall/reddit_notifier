from types import SimpleNamespace

import pytest

from reddit_notifier.matcher import (
    WatchItem,
    is_sale_post,
    match_post,
    normalize,
    typo_distance,
)


def post(title, body=""):
    return SimpleNamespace(title=title, body_text=body)


def only_match(p, item):
    matches = match_post(p, [item])
    assert len(matches) == 1, f"expected a match, got {matches}"
    return matches[0]


# --- normalizing -----------------------------------------------------------


def test_normalize_case_accents_punctuation():
    assert normalize("Guerlain PÊCHE  Mirage!") == "guerlain peche mirage"
    assert normalize("Vi-ride") == "vi ride"
    assert normalize("Tom &amp; Jerry") == "tom jerry"


# --- exact matching ----------------------------------------------------------


def test_case_insensitive():
    m = only_match(post("[WTS] VIRIDE 100ml"), WatchItem("Viride"))
    assert not m.fuzzy
    assert m.where == "title"


def test_accents_ignored_both_ways():
    only_match(post("[WTS] Peche Mirage"), WatchItem("Pêche Mirage"))
    only_match(post("[WTS] Pêche Mirage"), WatchItem("Peche Mirage"))


def test_macron_ignored():
    only_match(post("[WTS] Virere 50ml"), WatchItem("Virēre"))
    only_match(post("[WTS] VIRĒRE 50ml"), WatchItem("Virere"))


def test_similar_names_not_confused():
    # virere vs viride: 2 letters different, but 6-letter names allow only 1.
    assert match_post(post("[WTS] Virēre 50ml"), [WatchItem("Viride")]) == []


def test_explicit_variant():
    m = only_match(
        post("[WTS] Vi ride decant"), WatchItem("Viride", variants=["Vi ride"])
    )
    assert m.term == "Vi ride"
    assert not m.fuzzy


def test_body_only_match():
    p = post("[WTS] Big lot (bottles)", "Aventus 50ml\nViride 100ml, 90% full")
    m = only_match(p, WatchItem("Viride"))
    assert m.where == "body"


def test_no_match_inside_longer_word():
    item = WatchItem("Viride", fuzzy=False)
    assert match_post(post("[WTS] Virideum 50ml"), [item]) == []


def test_unrelated_post():
    assert match_post(post("[WTS] Initio Side Effect"), [WatchItem("Viride")]) == []


# --- fuzzy matching ----------------------------------------------------------


def test_typo_distance():
    assert typo_distance("viride", "viride") == 0
    assert typo_distance("virdie", "viride") == 1  # swapped letters
    assert typo_distance("virid", "viride") == 1  # missing letter
    assert typo_distance("orange", "orage") == 1


def test_fuzzy_swapped_letters():
    m = only_match(post("[WTS] Virdie 100ml"), WatchItem("Viride"))
    assert m.fuzzy
    assert m.matched_text == "virdie"


def test_fuzzy_split_word_without_listing_variant():
    m = only_match(post("[WTS] Vi ride 100ml"), WatchItem("Viride"))
    assert m.fuzzy
    assert m.matched_text == "vi ride"


def test_fuzzy_joined_words():
    m = only_match(post("[WTS] Santal33 bottle"), WatchItem("Santal 33"))
    assert m.fuzzy


def test_exact_preferred_over_fuzzy():
    p = post("[WTS] Virdie and Viride")
    assert not only_match(p, WatchItem("Viride")).fuzzy


def test_short_names_never_fuzzy():
    # "Orage" is one letter off "orange", a very common word in listings.
    assert match_post(post("[WTS] Orange blossom"), [WatchItem("Orage")]) == []


def test_fuzzy_can_be_turned_off():
    item = WatchItem("Viride", fuzzy=False)
    assert match_post(post("[WTS] Virdie 100ml"), [item]) == []


def test_two_typos_only_for_long_names():
    long_item = WatchItem("Amyris Femme")  # 11 letters: 2 typos allowed
    only_match(post("[WTS] Amiris Femm 70ml"), long_item)  # y->i, missing e
    assert match_post(post("[WTS] Amiris Fem 70ml"), [long_item]) == []  # 3 off
    short_item = WatchItem("Viride")  # 6 letters: only 1 typo
    assert match_post(post("[WTS] Vridie"), [short_item]) == []


# --- sale filter -------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("[WTS] Viride", True),
        ("[wts] Viride", True),
        ("[WTS] [WTT] Viride", True),
        ("[WTS/WTT] Viride", True),
        ("[WTT] Viride", False),
        ("[WTB] Viride", False),
        ("Viride for sale", False),
    ],
)
def test_is_sale_post(title, expected):
    assert is_sale_post(title) is expected


def test_sale_only_item_skips_trade_posts():
    item = WatchItem("Viride", sale_only=True)
    assert match_post(post("[WTT] Viride"), [item]) == []
    assert match_post(post("[WTB] Viride"), [item]) == []
    only_match(post("[WTS] [WTT] Viride"), item)


def test_non_sale_only_item_matches_any_post():
    only_match(post("[WTT] Viride"), WatchItem("Viride", sale_only=False))


def test_multiple_items():
    items = [WatchItem("Viride"), WatchItem("Orage"), WatchItem("Aventus")]
    p = post("[WTS] Lot", "Orage 100ml, Viride 50ml")
    names = {m.item.name for m in match_post(p, items)}
    assert names == {"Viride", "Orage"}
