"""Build per-market Google News RSS search feeds from open-market questions.

The feeder's dynamic loop calls build_query_feeds each tick with the current
open-market snapshot; each question becomes one Google News search query.
Every feed shares the same name/category so downstream metrics aggregate
(no per-market label cardinality).
"""
from urllib.parse import urlencode

from lib.feeds import Feed

_SEARCH_URL = "https://news.google.com/rss/search"
# Google News locale params: US English edition.
_LOCALE = {"hl": "en-US", "gl": "US", "ceid": "US:en"}

QUERY_FEED_NAME = "Google News Query"
QUERY_FEED_CATEGORY = "market_query"


def build_query_feeds(questions: list[str]) -> list[Feed]:
    """One Feed per question. The Feed's name/category are shared across all markets."""
    feeds: list[Feed] = []
    for question in questions:
        query = urlencode({"q": question, **_LOCALE})
        feeds.append(Feed(
            name=QUERY_FEED_NAME,
            url=f"{_SEARCH_URL}?{query}",
            category=QUERY_FEED_CATEGORY,
        ))
    return feeds
