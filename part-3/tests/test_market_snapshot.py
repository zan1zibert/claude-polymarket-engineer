"""Unit tests for MarketSnapshot publish/read against an in-memory fake redis.

The fake models only the GET/SET string ops the snapshot uses; it verifies the
full-overwrite semantics (closed markets vanish because publish replaces the
whole value) without needing a real Redis.
"""
from lib.market_snapshot import MarketSnapshot


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


def _snapshot():
    return MarketSnapshot.from_client(_FakeRedis(), "market_feed_snapshot")


def test_read_missing_key_returns_empty():
    assert _snapshot().read() == []


def test_publish_then_read_roundtrips():
    snap = _snapshot()
    snap.publish(["Will X?", "Will Y?"])
    assert snap.read() == ["Will X?", "Will Y?"]


def test_publish_is_full_overwrite():
    snap = _snapshot()
    snap.publish(["Will X?", "Will Y?"])
    snap.publish(["Will X?"])  # market 2 now closed → absent
    assert snap.read() == ["Will X?"]


def test_read_unparseable_value_returns_empty():
    fake = _FakeRedis()
    fake.set("market_feed_snapshot", "not json{")
    snap = MarketSnapshot.from_client(fake, "market_feed_snapshot")
    assert snap.read() == []
