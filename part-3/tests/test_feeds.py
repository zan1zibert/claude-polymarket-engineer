"""Guard: the static registry stays non-empty and well-formed.

No SYNC_TAG_FILTER restriction anymore -- markets are ingested across all
categories, so feeds are free to cover politics, world, crypto, finance, and
sports.
"""
from lib.feeds import FEEDS


def test_static_feeds_nonempty():
    assert FEEDS


def test_static_feeds_have_name_url_category():
    for f in FEEDS:
        assert f.name
        assert f.url
        assert f.category
