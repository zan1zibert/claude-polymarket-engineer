"""The dynamic-feed settings load with correct defaults and env overrides."""
from lib.config import load_settings


def test_dynamic_feed_defaults(monkeypatch):
    for var in (
        "MARKET_FEED_POLL_INTERVAL_SECONDS", "MARKET_FEED_SNAPSHOT_KEY",
        "MARKET_FEED_MAX_CONCURRENCY", "MARKET_FEED_FRESHNESS_WINDOW_MINUTES",
    ):
        monkeypatch.delenv(var, raising=False)
    s = load_settings()
    assert s.market_feed_poll_interval_seconds == 900
    assert s.market_feed_snapshot_key == "market_feed_snapshot"
    assert s.market_feed_max_concurrency == 8
    assert s.market_feed_freshness_window_minutes == 180


def test_dynamic_feed_env_overrides(monkeypatch):
    monkeypatch.setenv("MARKET_FEED_POLL_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("MARKET_FEED_MAX_CONCURRENCY", "4")
    s = load_settings()
    assert s.market_feed_poll_interval_seconds == 300
    assert s.market_feed_max_concurrency == 4
