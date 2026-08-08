"""Build per-market Google News RSS search feeds from open-market questions.

The feeder's dynamic loop calls build_query_feeds each tick with the current
open-market snapshot; each market becomes one Google News search query for its
exact question text. Every feed shares the same name/category so downstream
metrics aggregate (no per-market label cardinality) — the market_id is used
only to shape a distinct URL, not carried onto the Feed.
"""
from urllib.parse import urlencode

from lib.feeds import Feed

_SEARCH_URL = "https://news.google.com/rss/search"
# Google News locale params: US English edition.
_LOCALE = {"hl": "en-US", "gl": "US", "ceid": "US:en"}

QUERY_FEED_NAME = "Google News Query"
QUERY_FEED_CATEGORY = "market_query"


def build_query_feeds(markets: list[tuple[str, str]]) -> list[Feed]:
    """One Feed per (market_id, question) pair. market_id shapes the URL query
    only; the Feed's name/category are shared across all markets."""
    feeds: list[Feed] = []
    for _market_id, question in markets:
        query = urlencode({"q": question, **_LOCALE})
        feeds.append(Feed(
            name=QUERY_FEED_NAME,
            url=f"{_SEARCH_URL}?{query}",
            category=QUERY_FEED_CATEGORY,
        ))
    return feeds
