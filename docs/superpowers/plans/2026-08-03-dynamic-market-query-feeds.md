# Dynamic Market Query Feeds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the feeder per-market Google News query feeds — built from each open market's question — so ingestion matches Polymarket's specific wording, published by the syncer to Redis and fetched by a paced second feeder loop.

**Architecture:** The syncer (sole writer of the market set) writes a full `(id, question)` snapshot of open markets to one Redis key each cycle. A new, independent async loop in the feeder reads that key, builds one Google News RSS search feed per market, and fetches them on a paced launcher (evenly spread, concurrency-capped) through the existing dedup/queue path. Articles then flow through the unchanged worker pipeline (embed → top_k → Groq gate → Claude). No worker changes.

**Tech Stack:** Python 3, `httpx` (async), `redis-py`, `feedparser`, `prometheus_client`, `pytest`. Postgres/pgvector for the market table (read-only here).

## Global Constraints

- Work happens in `part-3/` — all paths below are relative to that directory.
- `SYNC_TAG_FILTER` stays `2` (Politics). Market scope is unchanged.
- The worker's retrieval/relevance/reevaluate pipeline is unchanged. No `market_id` is threaded from feed to worker.
- The feeder stays DB-free: it reads the market set from Redis, never Postgres.
- Full-overwrite snapshot only — no incremental feed store, no per-market deletes.
- DB integration tests are skipped unless `TEST_DATABASE_URL` is set (matches `tests/test_db.py`).
- Metrics: no per-market label series. All dynamic-feed articles use `source="Google News Query"`.
- Commit after every task with a conventional-commit message.

---

### Task 1: Trim non-politics static feeds

Remove the 5 feeds (Finance/Crypto/Sports) that can never match a politics-only market set, and add a guard test so they can't creep back.

**Files:**
- Modify: `lib/feeds.py` (remove BBC Business, CNBC Top News, Cointelegraph, CoinDesk, ESPN)
- Test: `tests/test_feeds.py` (create)

**Interfaces:**
- Consumes: `lib.feeds.FEEDS: list[Feed]`, `Feed.category: str`
- Produces: nothing new — `FEEDS` still exported, now politics/world only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feeds.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_feeds.py -v`
Expected: FAIL — `test_static_feeds_are_politics_or_world_only` reports `{'finance', 'crypto', 'sports'}`.

- [ ] **Step 3: Remove the 5 feeds**

In `lib/feeds.py`, delete the `# Finance / markets`, `# Crypto`, and `# Sports` blocks (BBC Business, CNBC Top News, Cointelegraph, CoinDesk, ESPN). Keep the `# World / general` and all `# Politics` feeds. The `FEEDS` list should end after the last politics feed (`Foreign Policy`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_feeds.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add part-3/lib/feeds.py part-3/tests/test_feeds.py
git commit -m "chore(feeder): drop non-politics static feeds; add category guard test"
```

---

### Task 2: `build_query_feeds` — market → Google News feed

Turn `(id, question)` pairs into `Feed` objects querying Google News RSS search.

**Files:**
- Create: `lib/market_feeds.py`
- Test: `tests/test_market_feeds.py`

**Interfaces:**
- Consumes: `lib.feeds.Feed` (`name`, `url`, `category`)
- Produces: `build_query_feeds(markets: list[tuple[str, str]]) -> list[Feed]` — one `Feed` per input pair; every returned feed has `name="Google News Query"`, `category="market_query"`, and a distinct `url`. Used by the feeder's dynamic loop (Task 8).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market_feeds.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_market_feeds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.market_feeds'`.

- [ ] **Step 3: Write the implementation**

```python
# lib/market_feeds.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_market_feeds.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add part-3/lib/market_feeds.py part-3/tests/test_market_feeds.py
git commit -m "feat(feeder): build_query_feeds — per-market Google News search feeds"
```

---

### Task 3: `MarketSnapshot` — Redis publish/read of the open-market set

A tiny wrapper over one Redis string key holding the `(id, question)` snapshot.

**Files:**
- Create: `lib/market_snapshot.py`
- Test: `tests/test_market_snapshot.py`

**Interfaces:**
- Consumes: a redis client (injected for tests) or a `redis_url`.
- Produces:
  - `MarketSnapshot(redis_url: str, key: str)` — constructs its own client.
  - `MarketSnapshot.from_client(client, key: str)` — classmethod for injection/tests.
  - `.publish(markets: list[tuple[str, str]]) -> None` — full overwrite, JSON list of `[id, question]`.
  - `.read() -> list[tuple[str, str]]` — the current snapshot, or `[]` if the key is missing/empty/unparseable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market_snapshot.py
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
    snap.publish([("1", "Will X?"), ("2", "Will Y?")])
    assert snap.read() == [("1", "Will X?"), ("2", "Will Y?")]


def test_publish_is_full_overwrite():
    snap = _snapshot()
    snap.publish([("1", "Will X?"), ("2", "Will Y?")])
    snap.publish([("1", "Will X?")])  # market 2 now closed → absent
    assert snap.read() == [("1", "Will X?")]


def test_read_unparseable_value_returns_empty():
    fake = _FakeRedis()
    fake.set("market_feed_snapshot", "not json{")
    snap = MarketSnapshot.from_client(fake, "market_feed_snapshot")
    assert snap.read() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_market_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.market_snapshot'`.

- [ ] **Step 3: Write the implementation**

```python
# lib/market_snapshot.py
"""Redis-backed snapshot of the open-market set: (id, question) pairs.

The syncer (the only writer of the market set) publishes the full open-market
list to one key each cycle; the feeder reads it to build per-market query feeds.
A full overwrite means closed markets simply drop out — there is no per-market
delete to get wrong, mirroring the DB's `WHERE NOT closed` semantics.
"""
import json
import logging

import redis

log = logging.getLogger("market_snapshot")


class MarketSnapshot:
    def __init__(self, redis_url: str, key: str):
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._key = key

    @classmethod
    def from_client(cls, client, key: str) -> "MarketSnapshot":
        self = cls.__new__(cls)
        self._r = client
        self._key = key
        return self

    def publish(self, markets: list[tuple[str, str]]) -> None:
        """Overwrite the snapshot with the current open-market set."""
        self._r.set(self._key, json.dumps([list(m) for m in markets]))

    def read(self) -> list[tuple[str, str]]:
        """Current snapshot, or [] if missing/empty/unparseable."""
        raw = self._r.get(self._key)
        if not raw:
            return []
        try:
            return [(str(i), str(q)) for i, q in json.loads(raw)]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("unparseable market snapshot: %s", exc)
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_market_snapshot.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add part-3/lib/market_snapshot.py part-3/tests/test_market_snapshot.py
git commit -m "feat: MarketSnapshot — full-overwrite Redis snapshot of open markets"
```

---

### Task 4: `Db.open_market_questions()`

Add the read the syncer uses to build the snapshot.

**Files:**
- Modify: `lib/db.py` (add method next to `open_market_ids`, ~line 247)
- Test: `tests/test_market_questions_db.py` (create — integration, `TEST_DATABASE_URL`-gated)

**Interfaces:**
- Consumes: the `markets` table (`id`, `question`, `closed` columns — already present).
- Produces: `Db.open_market_questions(self) -> list[tuple[str, str]]` — `(id, question)` for every market `WHERE NOT closed`. Used by the syncer (Task 6).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market_questions_db.py
"""Integration test for Db.open_market_questions.

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_market_questions_db.py

Skipped without TEST_DATABASE_URL so a bare pytest stays green.
"""
import os

import psycopg
import pytest

from db import migrate
from lib.db import Db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run open_market_questions DB test"
)

_IDS = ("utest_qOpen", "utest_qClosed")
_ZERO_VECTOR = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture(scope="module")
def _schema():
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


@pytest.fixture
def db(_schema):
    def _cleanup():
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
            cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (list(_IDS),))
    _cleanup()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO markets (id, question, description, embedding, closed) "
            "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
            ("utest_qOpen", "Will the open market resolve yes?", "d", _ZERO_VECTOR, False,
             "utest_qClosed", "Will the closed market resolve yes?", "d", _ZERO_VECTOR, True),
        )
    d = Db(TEST_DATABASE_URL)
    yield d
    _cleanup()


def test_open_market_questions_returns_only_open_with_question(db):
    rows = dict(db.open_market_questions())
    assert rows.get("utest_qOpen") == "Will the open market resolve yes?"
    assert "utest_qClosed" not in rows
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm python -m pytest tests/test_market_questions_db.py -v`
Expected: FAIL — `AttributeError: 'Db' object has no attribute 'open_market_questions'`.
(If no test DB is available, the module skips — implement Step 3 and rely on the reviewer/CI DB run.)

- [ ] **Step 3: Add the method**

In `lib/db.py`, immediately after `open_market_ids` (ends ~line 247), add:

```python
    def open_market_questions(self) -> list[tuple[str, str]]:
        """(id, question) for every market still open — the set the syncer
        publishes as the feeder's query-feed snapshot. Same rows as
        open_market_ids(), plus the question text needed to build a query."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT id, question FROM markets WHERE NOT closed")
            return [(r[0], r[1]) for r in cur.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm python -m pytest tests/test_market_questions_db.py -v`
Expected: PASS (1 test). Without a DB: `SKIPPED`.

- [ ] **Step 5: Commit**

```bash
git add part-3/lib/db.py part-3/tests/test_market_questions_db.py
git commit -m "feat(db): open_market_questions — (id, question) for open markets"
```

---

### Task 5: Config knobs + metric gauge

Add the four new settings and the dynamic-feed gauge. Scaffolding for Tasks 6–8.

**Files:**
- Modify: `lib/config.py` (add fields to `Settings` + `load_settings`)
- Modify: `lib/metrics.py` (add `FEEDER_MARKET_QUERY_FEEDS`)
- Test: `tests/test_config_market_feeds.py` (create)

**Interfaces:**
- Produces on `Settings`:
  - `market_feed_poll_interval_seconds: int` (env `MARKET_FEED_POLL_INTERVAL_SECONDS`, default `900`)
  - `market_feed_snapshot_key: str` (env `MARKET_FEED_SNAPSHOT_KEY`, default `"market_feed_snapshot"`)
  - `market_feed_max_concurrency: int` (env `MARKET_FEED_MAX_CONCURRENCY`, default `8`)
  - `market_feed_freshness_window_minutes: int` (env `MARKET_FEED_FRESHNESS_WINDOW_MINUTES`, default `180`)
- Produces in `metrics`: `FEEDER_MARKET_QUERY_FEEDS` (Gauge, no labels).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_market_feeds.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_config_market_feeds.py -v`
Expected: FAIL — `TypeError` / `AttributeError` for the missing `Settings` fields.

- [ ] **Step 3a: Add the `Settings` fields**

In `lib/config.py`, inside the `@dataclass Settings`, in the market-syncer section (after `scorer_interval_seconds` or grouped under a new `# --- dynamic market feeds ---` comment before the closing), add:

```python
    # --- dynamic market feeds (feeder second loop) ---
    market_feed_poll_interval_seconds: int   # dynamic-loop interval
    market_feed_snapshot_key: str            # Redis key: syncer writes, feeder reads
    market_feed_max_concurrency: int         # cap on in-flight dynamic fetches
    market_feed_freshness_window_minutes: int  # freshness cutoff for dynamic articles
```

- [ ] **Step 3b: Populate them in `load_settings`**

In `lib/config.py`, in the `return Settings(...)` call (before the closing `)`), add:

```python
        market_feed_poll_interval_seconds=int(
            os.environ.get("MARKET_FEED_POLL_INTERVAL_SECONDS", "900")),
        market_feed_snapshot_key=os.environ.get(
            "MARKET_FEED_SNAPSHOT_KEY", "market_feed_snapshot"),
        market_feed_max_concurrency=int(
            os.environ.get("MARKET_FEED_MAX_CONCURRENCY", "8")),
        market_feed_freshness_window_minutes=int(
            os.environ.get("MARKET_FEED_FRESHNESS_WINDOW_MINUTES", "180")),
```

- [ ] **Step 3c: Add the metric**

In `lib/metrics.py`, in the `# --- feeder ---` block (after `FEEDER_RSS_FEEDS`), add:

```python
FEEDER_MARKET_QUERY_FEEDS = Gauge(
    "feeder_market_query_feeds",
    "Number of per-market Google News query feeds active after the last dynamic refresh",
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_config_market_feeds.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add part-3/lib/config.py part-3/lib/metrics.py part-3/tests/test_config_market_feeds.py
git commit -m "feat(config): dynamic market-feed settings + query-feed gauge"
```

---

### Task 6: Syncer publishes the snapshot each cycle

After a successful sync, write the open-market snapshot to Redis.

**Files:**
- Modify: `services/syncer/main.py` (import `MarketSnapshot`; construct in `run`; publish after `sync_once`)
- Test: `tests/test_syncer_snapshot.py` (create)

**Interfaces:**
- Consumes: `Db.open_market_questions()` (Task 4), `MarketSnapshot.publish` (Task 3), `settings.redis_url`, `settings.market_feed_snapshot_key` (Task 5).
- Produces: `publish_snapshot(db, snapshot) -> int` — reads open-market questions, publishes them, returns the count (for logging/metrics and testability).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_syncer_snapshot.py
"""publish_snapshot reads open-market questions and overwrites the Redis snapshot."""
from unittest.mock import MagicMock

from services.syncer.main import publish_snapshot


def test_publish_snapshot_publishes_open_market_questions():
    db = MagicMock()
    db.open_market_questions.return_value = [("1", "Will X?"), ("2", "Will Y?")]
    snapshot = MagicMock()

    count = publish_snapshot(db, snapshot)

    assert count == 2
    snapshot.publish.assert_called_once_with([("1", "Will X?"), ("2", "Will Y?")])


def test_publish_snapshot_handles_empty_open_set():
    db = MagicMock()
    db.open_market_questions.return_value = []
    snapshot = MagicMock()

    count = publish_snapshot(db, snapshot)

    assert count == 0
    snapshot.publish.assert_called_once_with([])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_syncer_snapshot.py -v`
Expected: FAIL — `ImportError: cannot import name 'publish_snapshot'`.

- [ ] **Step 3a: Add `publish_snapshot` and the import**

In `services/syncer/main.py`, add the import near the others:

```python
from lib.market_snapshot import MarketSnapshot
```

Add the function (module level, e.g. after `sync_once`):

```python
def publish_snapshot(db: Db, snapshot: MarketSnapshot) -> int:
    """Overwrite the feeder's open-market snapshot from the current DB state.
    Returns the number of open markets published."""
    markets = db.open_market_questions()
    snapshot.publish(markets)
    return len(markets)
```

- [ ] **Step 3b: Wire it into `run`**

In `services/syncer/main.py` `run()`, after `db = _wait_for_db(settings)` (~line 166), construct the snapshot:

```python
    snapshot = MarketSnapshot(settings.redis_url, settings.market_feed_snapshot_key)
```

Then inside the `while not stop.is_set():` loop, after the `counts = db.corpus_counts()` / gauge-setting block and before the `log.info("synced: ...")` line, publish:

```python
                published = publish_snapshot(db, snapshot)
                log.info("published market snapshot: %d open markets", published)
```

(Publishing sits inside the same `try` as `sync_once`, so a Redis failure is caught by the existing `except Exception: log.exception("sync cycle failed")` and the loop continues.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_syncer_snapshot.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add part-3/services/syncer/main.py part-3/tests/test_syncer_snapshot.py
git commit -m "feat(syncer): publish open-market snapshot to Redis each cycle"
```

---

### Task 7: Paced launcher for dynamic fetches

An async helper that fetches feeds spread across the interval, concurrency-capped.

**Files:**
- Modify: `services/feeder/main.py` (add `fetch_feeds_paced`)
- Test: `tests/test_feeder_pacing.py` (create)

**Interfaces:**
- Consumes: `fetch_feed(client, feed)` (existing, returns `list[tuple[str, Article]]`), `lib.feeds.Feed`.
- Produces:
  `async def fetch_feeds_paced(client, feeds, interval_seconds, max_concurrency, *, fetch=fetch_feed, sleep=asyncio.sleep) -> list[tuple[str, Article]]`
  — launches one fetch per feed spaced by `interval_seconds / len(feeds)`, at most `max_concurrency` in flight, returns all `(key, Article)` pairs flattened. `fetch`/`sleep` are injectable for tests. Returns `[]` for empty `feeds`. Used by the dynamic loop (Task 8).

- [ ] **Step 0 (once): ensure `pytest-asyncio` is available**

This is the first task with async tests. `pytest-asyncio` is not currently installed. Add it to the dev/test requirements and install:

```bash
cd part-3 && python -m pip install pytest-asyncio
```

If the repo has no test-requirements file, add `pytest-asyncio` to whichever requirements file the other test deps (`pytest`) live in. Keep the explicit `@pytest.mark.asyncio` markers used in the tests below (no `asyncio_mode=auto` assumed); if a config already sets `asyncio_mode`, leave it and keep the markers consistent.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feeder_pacing.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_feeder_pacing.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_feeds_paced'`. (If `pytest-asyncio` is missing, install it — see note in Task 8 Step 0.)

- [ ] **Step 3: Implement `fetch_feeds_paced`**

In `services/feeder/main.py`, add (after `fetch_feed`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_feeder_pacing.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add part-3/services/feeder/main.py part-3/tests/test_feeder_pacing.py
git commit -m "feat(feeder): fetch_feeds_paced — spread + concurrency-capped fetches"
```

---

### Task 8: Feeder dynamic loop — read snapshot, fetch, enqueue

Wire the snapshot read → build feeds → paced fetch → dedup/freshness/queue path, running as a second loop alongside the static one. Extract the shared enqueue step so both loops use it.

**Files:**
- Modify: `services/feeder/main.py`
- Test: `tests/test_feeder_dynamic.py` (create)

**Interfaces:**
- Consumes: `build_query_feeds` (Task 2), `MarketSnapshot.read` (Task 3), `fetch_feeds_paced` (Task 7), the four `market_feed_*` settings (Task 5), `metrics.FEEDER_MARKET_QUERY_FEEDS` (Task 5).
- Produces:
  - `_enqueue(results, queue, dedup, cutoff) -> int` — freshness+dedup+push for a list of `(key, Article)` pairs, given an explicit `cutoff` datetime; returns count pushed.
  - `async def poll_dynamic_once(client, queue, dedup, snapshot, settings) -> int` — one dynamic cycle.

- [ ] **Step 1: Write the failing test**

(`pytest-asyncio` was installed in Task 7 Step 0.)

```python
# tests/test_feeder_dynamic.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd part-3 && python -m pytest tests/test_feeder_dynamic.py -v`
Expected: FAIL — `AttributeError: module 'services.feeder.main' has no attribute 'poll_dynamic_once'`.

- [ ] **Step 3a: Extract `_enqueue` and refactor `poll_once`**

In `services/feeder/main.py`, add:

```python
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
```

Refactor `poll_once` to use it (replace its inner push loop):

```python
async def poll_once(
    client: httpx.AsyncClient, queue: NewsQueue, dedup: Dedup, settings: Settings
) -> int:
    """Run one full poll across all static feeds. Returns new articles enqueued."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.freshness_window_minutes)
    results = await asyncio.gather(*(fetch_feed(client, f) for f in FEEDS))
    flat = [pair for sub in results for pair in sub]
    return _enqueue(flat, queue, dedup, cutoff)
```

- [ ] **Step 3b: Add `poll_dynamic_once`**

Add imports at the top of `services/feeder/main.py`:

```python
from lib.market_feeds import build_query_feeds
from lib.market_snapshot import MarketSnapshot
```

Add the dynamic cycle:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd part-3 && python -m pytest tests/test_feeder_dynamic.py tests/test_feeder_pacing.py -v`
Expected: PASS (dynamic: 2, pacing: 3).

- [ ] **Step 5: Commit**

```bash
git add part-3/services/feeder/main.py part-3/tests/test_feeder_dynamic.py
git commit -m "feat(feeder): dynamic per-market query-feed poll cycle"
```

---

### Task 9: Run both feeder loops concurrently

Start the dynamic loop alongside the static loop in `run`, and register the dynamic source label.

**Files:**
- Modify: `services/feeder/main.py` (`run`)
- Test: covered by Tasks 7–8 (the loop bodies) + a manual `--once` smoke check below.

**Interfaces:**
- Consumes: `poll_once`, `poll_dynamic_once`, `MarketSnapshot`, the `market_feed_*` settings.
- Produces: a `run` that drives both loops until the shutdown signal.

- [ ] **Step 1: Pre-register the dynamic source label + snapshot**

In `run()` (after the existing per-feed label registration loop, ~line 174), add:

```python
    # Dynamic-feed articles all share one source label (no per-market cardinality).
    metrics.FEEDER_ARTICLES_FETCHED.labels(source="Google News Query")
    metrics.FEEDER_ARTICLES_PUSHED.labels(source="Google News Query")
    snapshot = MarketSnapshot(settings.redis_url, settings.market_feed_snapshot_key)
```

- [ ] **Step 2: Split the run loop into two coroutines**

Replace the single `while not stop.is_set(): ...` block in `run` with two nested coroutines driven by `asyncio.gather`, sharing the one `client` (for connection reuse) and the `stop` event. Keep the static loop's existing body verbatim inside `_static_loop`.

```python
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
```

- [ ] **Step 3: Full test suite (no-DB) passes**

Run: `cd part-3 && python -m pytest -q`
Expected: PASS (DB-integration modules SKIPPED without `TEST_DATABASE_URL`).

- [ ] **Step 4: Manual smoke — one dynamic cycle end-to-end**

With Redis running and a snapshot present (or seed one quickly), run the feeder once and confirm no crash and the gauge populates. Seed a snapshot from a Python shell if needed:

```bash
cd part-3 && python -c "
from lib.config import load_settings
from lib.market_snapshot import MarketSnapshot
s = load_settings()
MarketSnapshot(s.redis_url, s.market_feed_snapshot_key).publish([('1','Will the US government shut down before October?')])
print('seeded')
"
python -m services.feeder.main --once
```

Expected: log lines for both the static and `dynamic: pushed N ... from 1 query feeds`, no traceback.

- [ ] **Step 5: Commit**

```bash
git add part-3/services/feeder/main.py
git commit -m "feat(feeder): run static + dynamic poll loops concurrently"
```

---

## Self-Review

**Spec coverage:**
- Static feed cleanup → Task 1. ✅
- `build_query_feeds` / Google News search URL → Task 2. ✅
- Syncer publishes full-overwrite snapshot to Redis → Tasks 3 (snapshot) + 6 (syncer wiring). ✅
- `Db.open_market_questions` → Task 4. ✅
- Config knobs (`MARKET_FEED_POLL_INTERVAL_SECONDS`, `_SNAPSHOT_KEY`, `_MAX_CONCURRENCY`, `_FRESHNESS_WINDOW_MINUTES`) → Task 5. ✅
- `FEEDER_MARKET_QUERY_FEEDS` gauge, shared `source` label (no per-market cardinality) → Task 5 (gauge) + Task 2/9 (shared label). ✅
- Paced launcher + concurrency cap → Task 7. ✅
- Dynamic freshness window → Task 8 (`_enqueue` cutoff) + Task 5 (setting). ✅
- Feeder stays DB-free (reads snapshot, not Postgres) → Tasks 8–9 use `MarketSnapshot` only. ✅
- Two independent loops, never crash on missing snapshot → Task 8 (empty read → `[]`) + Task 9 (per-loop `try/except`). ✅
- Connection reuse (shared `AsyncClient`, no HTTP/2) → Task 9 shares the one client; no `http2=` added. ✅
- Testing: build_query_feeds (Task 2), snapshot overwrite (Task 3), open_market_questions integration (Task 4), pacing concurrency (Task 7), dynamic loop + missing snapshot (Task 8). ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✅

**Type consistency:** `build_query_feeds(list[tuple[str,str]]) -> list[Feed]`, `MarketSnapshot.read() -> list[tuple[str,str]]`/`.publish(list[tuple[str,str]])`, `open_market_questions() -> list[tuple[str,str]]`, `fetch_feeds_paced(...) -> list[tuple[str, Article]]`, `_enqueue(results, queue, dedup, cutoff) -> int`, `poll_dynamic_once(...) -> int` — names and types line up across Tasks 2→8. `source="Google News Query"` matches `QUERY_FEED_NAME` in Task 2. ✅
