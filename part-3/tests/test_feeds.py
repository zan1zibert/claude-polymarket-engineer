"""Guard: the static registry only carries categories that a politics-only
market set can actually match. Crypto/finance/sports feeds are removed because
SYNC_TAG_FILTER=2 (Politics) means no market in those categories is ever stored.
"""
from lib.feeds import FEEDS

_ALLOWED_CATEGORIES = {"world", "politics"}


def test_static_feeds_are_politics_or_world_only():
    bad = {f.category for f in FEEDS} - _ALLOWED_CATEGORIES
    assert not bad, f"unexpected feed categories: {bad}"


def test_removed_feeds_are_absent():
    names = {f.name for f in FEEDS}
    for removed in ("BBC Business", "CNBC Top News", "Cointelegraph", "CoinDesk", "ESPN"):
        assert removed not in names
