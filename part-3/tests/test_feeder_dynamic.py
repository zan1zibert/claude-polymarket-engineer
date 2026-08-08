"""The dynamic loop reads the snapshot, builds one feed per market, fetches them
through the paced launcher, and pushes survivors — and never crashes on a
missing/empty snapshot.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lib.schemas import Article
import services.feeder.main as feeder


def _settings():
    return SimpleNamespace(
        market_feed_poll_interval_seconds=900,
        market_feed_max_concurrency=8,
        market_feed_freshness_window_minutes=180,
    )


def _article(url):
    return Article(
        url=url, title="t", summary="s", source="Google News Query",
        category="market_query", published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.mark.asyncio
async def test_dynamic_loop_fetches_per_market_and_pushes(monkeypatch):
    snapshot = MagicMock()
    snapshot.read.return_value = [("1", "Will X?"), ("2", "Will Y?")]
    queue = MagicMock()
    dedup = MagicMock()
    dedup.is_new.return_value = True

    async def fake_paced(client, feeds, interval_seconds, max_concurrency):
        # one article per built feed
        return [(f"k{i}", _article(f"https://n/{i}")) for i, _ in enumerate(feeds)]

    monkeypatch.setattr(feeder, "fetch_feeds_paced", fake_paced)

    pushed = await feeder.poll_dynamic_once(
        client=None, queue=queue, dedup=dedup, snapshot=snapshot, settings=_settings(),
    )

    assert pushed == 2
    assert queue.push.call_count == 2
    assert feeder.metrics.FEEDER_MARKET_QUERY_FEEDS._value.get() == 2


@pytest.mark.asyncio
async def test_dynamic_loop_empty_snapshot_pushes_nothing(monkeypatch):
    snapshot = MagicMock()
    snapshot.read.return_value = []
    queue = MagicMock()
    dedup = MagicMock()

    async def fake_paced(client, feeds, interval_seconds, max_concurrency):
        assert feeds == []
        return []

    monkeypatch.setattr(feeder, "fetch_feeds_paced", fake_paced)

    pushed = await feeder.poll_dynamic_once(
        client=None, queue=queue, dedup=dedup, snapshot=snapshot, settings=_settings(),
    )

    assert pushed == 0
    queue.push.assert_not_called()
