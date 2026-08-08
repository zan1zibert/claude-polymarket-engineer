"""News feeder — the producer.

Responsibilities (deliberately narrow):
  1. Poll a registry of RSS feeds concurrently.
  2. Drop articles older than the freshness window (avoids enqueuing history on
     first run) and articles we've already seen (Redis dedup).
  3. Push the survivors onto the Redis queue for the worker to consume.

It does NOT embed, match, score, or call Claude. Keeping it dumb means it can
crash and restart cheaply, and the expensive/fallible work lives downstream.

Run:
    python -m services.feeder.main          # poll forever
    python -m services.feeder.main --once    # one cycle then exit (handy for dev)
"""
import asyncio
import html
import logging
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import httpx
from dotenv import load_dotenv

from lib.config import Settings, load_settings
from lib.dedup import Dedup, normalize_url
from lib.feeds import FEEDS, Feed
from lib import metrics
from lib.market_feeds import build_query_feeds
from lib.market_snapshot import MarketSnapshot
from lib.queue import NewsQueue
from lib.schemas import Article

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
log = logging.getLogger("feeder")

# Per-feed conditional-GET validators: feed_url -> (etag, last_modified).
# In-memory is fine because the feeder is a singleton; on restart we simply
# re-fetch once and dedup absorbs the duplicates.
_validators: dict[str, tuple[Optional[str], Optional[str]]] = {}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _parse_published(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _dedup_key(entry, link: str) -> str:
    """Stable identity for an article.

    Prefer the feed-provided guid (`entry.id`) — the publisher guarantees it's
    the same across every poll for the same item. Only when it's missing do we
    fall back to a normalized URL, which collapses tracking-param variants.
    """
    guid = entry.get("id")
    return guid if guid else normalize_url(link)


def _entry_to_article(entry, feed: Feed) -> Optional[tuple[str, Article]]:
    """Return (dedup_key, Article), or None if the entry is unusable.

    The dedup key is kept separate from the stored url: we dedup on guid/
    normalized-url, but the Article keeps the original link so it stays openable.
    """
    url = entry.get("link")
    title = entry.get("title")
    if not url or not title:
        return None
    published = _parse_published(entry)
    article = Article(
        url=url,
        title=title.strip(),
        summary=_strip_html(entry.get("summary", "") or "")[:1000],
        source=feed.name,
        category=feed.category,
        published_at=published.isoformat() if published else None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    return _dedup_key(entry, url), article


async def fetch_feed(client: httpx.AsyncClient, feed: Feed) -> list[tuple[str, Article]]:
    """Fetch one feed with a conditional GET; return (dedup_key, Article) pairs ([] if unchanged/failed)."""
    etag, last_modified = _validators.get(feed.url, (None, None))
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        resp = await client.get(feed.url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as e:
        log.warning("fetch failed [%s]: %s", feed.name, e)
        return []

    if resp.status_code == 304:
        return []  # unchanged since last poll
    if resp.status_code != 200:
        log.warning("[%s] HTTP %s", feed.name, resp.status_code)
        return []

    _validators[feed.url] = (resp.headers.get("ETag"), resp.headers.get("Last-Modified"))

    parsed = feedparser.parse(resp.content)
    pairs = [pair for e in parsed.entries if (pair := _entry_to_article(e, feed))]
    metrics.FEEDER_ARTICLES_FETCHED.labels(source=feed.name).inc(len(pairs))
    return pairs


async def fetch_feeds_paced(
    client: httpx.AsyncClient,
    feeds: list[Feed],
    interval_seconds: float,
    max_concurrency: int,
    *,
    fetch=fetch_feed,
    sleep=asyncio.sleep,
) -> list[tuple[str, Article]]:
    """Fetch every feed once, launches spread evenly across interval_seconds
    (delay = interval / N) and bounded to max_concurrency in flight.

    Spreading avoids bursting hundreds of requests at a single host
    (news.google.com) every cycle; the semaphore is a floor guard for when the
    market count grows large enough that even spacing can't keep the launches
    apart. fetch/sleep are injectable for tests.
    """
    if not feeds:
        return []
    delay = interval_seconds / len(feeds)
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(feed: Feed) -> list[tuple[str, Article]]:
        async with sem:
            return await fetch(client, feed)

    tasks = []
    for i, feed in enumerate(feeds):
        if i:
            await sleep(delay)
        tasks.append(asyncio.create_task(_one(feed)))
    results = await asyncio.gather(*tasks)
    return [pair for sub in results for pair in sub]


def _enqueue(
    results: list[tuple[str, Article]],
    queue: NewsQueue,
    dedup: Dedup,
    cutoff: datetime,
) -> int:
    """Apply the freshness gate + dedup, push survivors, return count pushed.
    Shared by the static and dynamic poll loops; `cutoff` lets each loop use
    its own freshness window."""
    pushed = 0
    for key, a in results:
        if a.published_at and datetime.fromisoformat(a.published_at) < cutoff:
            continue
        if not dedup.is_new(key):
            continue
        queue.push(a)
        metrics.FEEDER_ARTICLES_PUSHED.labels(source=a.source).inc()
        pushed += 1
    return pushed


async def poll_once(
    client: httpx.AsyncClient, queue: NewsQueue, dedup: Dedup, settings: Settings
) -> int:
    """Run one full poll across all static feeds. Returns new articles enqueued."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.freshness_window_minutes)
    results = await asyncio.gather(*(fetch_feed(client, f) for f in FEEDS))
    flat = [pair for sub in results for pair in sub]
    return _enqueue(flat, queue, dedup, cutoff)


async def poll_dynamic_once(
    client: httpx.AsyncClient,
    queue: NewsQueue,
    dedup: Dedup,
    snapshot: MarketSnapshot,
    settings: Settings,
) -> int:
    """One dynamic cycle: read the open-market snapshot, build a Google News
    query feed per market, fetch them paced/capped, enqueue the survivors."""
    feeds = build_query_feeds(snapshot.read())
    metrics.FEEDER_MARKET_QUERY_FEEDS.set(len(feeds))
    results = await fetch_feeds_paced(
        client, feeds,
        settings.market_feed_poll_interval_seconds,
        settings.market_feed_max_concurrency,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.market_feed_freshness_window_minutes)
    return _enqueue(results, queue, dedup, cutoff)


def _wait_for_redis(queue: NewsQueue, attempts: int = 30) -> None:
    for i in range(attempts):
        try:
            if queue.ping():
                return
        except Exception:
            pass
        log.info("waiting for redis... (%d/%d)", i + 1, attempts)
        time.sleep(1)
    raise RuntimeError("redis not reachable")


async def run(once: bool = False) -> None:
    load_dotenv()
    settings = load_settings()
    queue = NewsQueue(settings.redis_url, settings.queue_key)
    dedup = Dedup(settings.redis_url, settings.dedup_ttl_seconds)

    _wait_for_redis(queue)

    metrics.start_metrics_server(settings.metrics_port)
    metrics.FEEDER_RSS_FEEDS.set(len(FEEDS))
    # Register every feed's label series up front so all feeds appear in
    # dashboards immediately, at 0, rather than only after their first article.
    for f in FEEDS:
        metrics.FEEDER_ARTICLES_FETCHED.labels(source=f.name)
        metrics.FEEDER_ARTICLES_PUSHED.labels(source=f.name)

    # Dynamic-feed articles all share one source label (no per-market cardinality).
    metrics.FEEDER_ARTICLES_FETCHED.labels(source="Google News Query")
    metrics.FEEDER_ARTICLES_PUSHED.labels(source="Google News Query")
    snapshot = MarketSnapshot(settings.redis_url, settings.market_feed_snapshot_key)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "feeder started: %d feeds, poll every %ds, freshness %dm",
        len(FEEDS), settings.poll_interval_seconds, settings.freshness_window_minutes,
    )

    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        async def _static_loop() -> None:
            while not stop.is_set():
                try:
                    pushed = await poll_once(client, queue, dedup, settings)
                    depth = queue.depth()
                    metrics.FEEDER_POLL_CYCLES.inc()
                    metrics.NEWS_QUEUE_DEPTH.set(depth)
                    log.info("pushed %d new articles (queue depth %d)", pushed, depth)
                except Exception:
                    log.exception("poll cycle failed")
                if once:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass

        async def _dynamic_loop() -> None:
            while not stop.is_set():
                try:
                    pushed = await poll_dynamic_once(client, queue, dedup, snapshot, settings)
                    log.info("dynamic: pushed %d new articles from %d query feeds",
                             pushed, int(metrics.FEEDER_MARKET_QUERY_FEEDS._value.get()))
                except Exception:
                    log.exception("dynamic poll cycle failed")
                if once:
                    break
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.market_feed_poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass

        await asyncio.gather(_static_loop(), _dynamic_loop())

    log.info("feeder stopped")


if __name__ == "__main__":
    asyncio.run(run(once="--once" in sys.argv))
