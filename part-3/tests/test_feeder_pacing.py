"""fetch_feeds_paced respects the concurrency cap and fetches every feed once."""
import asyncio

import pytest

from lib.feeds import Feed
from services.feeder.main import fetch_feeds_paced


def _feeds(n):
    return [Feed(name="Google News Query", url=f"https://x/{i}", category="market_query")
            for i in range(n)]


@pytest.mark.asyncio
async def test_all_feeds_fetched_once_and_results_flattened():
    seen = []

    async def fake_fetch(client, feed):
        seen.append(feed.url)
        return [(feed.url, f"article-for-{feed.url}")]

    async def no_sleep(_):
        return None

    results = await fetch_feeds_paced(
        client=None, feeds=_feeds(5), interval_seconds=1.0, max_concurrency=8,
        fetch=fake_fetch, sleep=no_sleep,
    )

    assert len(seen) == 5
    assert len(results) == 5  # one pair per feed, flattened


@pytest.mark.asyncio
async def test_never_exceeds_max_concurrency():
    in_flight = 0
    peak = 0

    async def fake_fetch(client, feed):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so others can start
        await asyncio.sleep(0)
        in_flight -= 1
        return []

    async def no_sleep(_):
        return None

    await fetch_feeds_paced(
        client=None, feeds=_feeds(20), interval_seconds=0.0, max_concurrency=3,
        fetch=fake_fetch, sleep=no_sleep,
    )

    assert peak <= 3


@pytest.mark.asyncio
async def test_empty_feeds_returns_empty():
    async def fake_fetch(client, feed):  # pragma: no cover - must not be called
        raise AssertionError("should not fetch")

    out = await fetch_feeds_paced(
        client=None, feeds=[], interval_seconds=1.0, max_concurrency=8,
        fetch=fake_fetch, sleep=asyncio.sleep,
    )
    assert out == []
