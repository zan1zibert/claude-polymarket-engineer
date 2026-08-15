"""Unit tests for DirtyMarkets against an in-memory fake redis.

The fake models only the set ops used (SADD/SPOP/SCARD), which is enough to
verify the property the whole design turns on: adding the same market twice
leaves ONE pending entry, so repeat notifications collapse for free. A Redis list
cannot do that without an O(n) scan-and-LREM, which is why this is a set.

Same fake-client injection pattern as tests/test_market_snapshot.py.
"""
from lib.queue import DirtyMarkets


class _FakeRedis:
    def __init__(self):
        self.sets = {}

    def sadd(self, key, *values):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(values)
        return len(s) - before

    def spop(self, key):
        s = self.sets.get(key)
        if not s:
            return None
        return s.pop()

    def scard(self, key):
        return len(self.sets.get(key, ()))


def _dirty():
    return DirtyMarkets.from_client(_FakeRedis(), "belief_dirty")


def test_pop_on_empty_returns_none():
    assert _dirty().pop() is None


def test_add_then_pop_roundtrips():
    d = _dirty()
    d.add("123")
    assert d.pop() == "123"
    assert d.pop() is None


def test_adding_the_same_market_twice_collapses():
    """The reason this is a set: repeat notifications dedupe by construction."""
    d = _dirty()
    d.add("123")
    d.add("123")
    d.add("123")
    assert d.depth() == 1
    assert d.pop() == "123"
    assert d.pop() is None


def test_distinct_markets_are_all_kept():
    d = _dirty()
    d.add("123")
    d.add("456")
    assert d.depth() == 2
    assert {d.pop(), d.pop()} == {"123", "456"}


def test_depth_reflects_pending_count():
    d = _dirty()
    assert d.depth() == 0
    d.add("123")
    assert d.depth() == 1
    d.pop()
    assert d.depth() == 0
