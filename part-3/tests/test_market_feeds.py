"""Unit tests for build_query_feeds: URL construction/encoding and empty input."""
from urllib.parse import parse_qs, urlsplit

from lib.market_feeds import build_query_feeds

_BASE = "news.google.com"


def test_empty_input_returns_empty_list():
    assert build_query_feeds([]) == []


def test_one_feed_per_market():
    feeds = build_query_feeds([("1", "Will X happen?"), ("2", "Will Y happen?")])
    assert len(feeds) == 2


def test_feed_shares_name_and_category():
    (feed,) = build_query_feeds([("1", "Will X happen?")])
    assert feed.name == "Google News Query"
    assert feed.category == "market_query"


def test_question_is_url_encoded_into_q_param():
    (feed,) = build_query_feeds([("1", 'Will "the Fed" cut rates by 50%?')])
    parts = urlsplit(feed.url)
    assert parts.netloc == _BASE
    q = parse_qs(parts.query)["q"][0]
    assert q == 'Will "the Fed" cut rates by 50%?'  # decoded round-trips exactly


def test_distinct_questions_give_distinct_urls():
    feeds = build_query_feeds([("1", "Question A"), ("2", "Question B")])
    assert feeds[0].url != feeds[1].url
