# Dynamic Market Query Feeds — Design

**Date:** 2026-08-03
**Status:** Approved (pending implementation plan)

## Problem

The feeder polls a static registry of generic RSS feeds (`lib/feeds.py`) — BBC,
Guardian, NPR, Politico, etc. Two problems, discovered while scoping increased
Groq relevance-filtering capacity:

1. **Category mismatch.** `SYNC_TAG_FILTER` (the market-syncer's Gamma tag
   filter) is `2` (Politics) by design — this product deliberately scopes to
   political belief-tracking. But `feeds.py` also carries Finance, Crypto, and
   Sports feeds. Articles from those categories can never match a tracked
   market, because no market in that category is ever ingested. They're pure
   noise in the queue.
2. **Specificity mismatch.** Even within politics, generic news headlines
   rarely map cleanly onto Polymarket's narrow, specific question wording
   (e.g. "Will the Senate confirm X by March 1"). A generic "politics" feed
   surfaces broad narrative coverage, not the precise event triggers a
   belief-update pipeline needs. This is the harder problem: paying for X/Twitter
   API access ($200/mo) would help via real-time, granular signal, but is out
   of proportion to the actual gap.

## Goal

Close the specificity gap for free: generate a targeted RSS query feed *per
open market*, built from that market's own question text, using Google News'
public RSS search endpoint (no API key, no paid tier). Also remove the feeds
that can structurally never match anything, given politics-only market scope.

## Non-goals

- No X/Twitter ingestion.
- No Reddit/Bluesky/Mastodon or other social-signal sources — worth
  considering later if this still doesn't produce enough specific matches, but
  out of scope here.
- No NLP/entity extraction from market question text in v1 — the raw question
  string is the query. If recall turns out poor, refining query construction
  is a fast follow-up, not blocking this pass.
- No change to `SYNC_TAG_FILTER` or market scope — politics-only remains
  intentional.
- No change to the worker's retrieval/relevance/reevaluate pipeline — dynamic
  feeds are just another article source hitting the same queue.

## Design

### Static feed cleanup

Remove from `lib/feeds.py`: BBC Business, CNBC Top News (Finance), Cointelegraph,
CoinDesk (Crypto), ESPN (Sports) — five feeds that can never produce a match
against a politics-only market set. The remaining 12 world/politics feeds stay
as the broad-discovery layer.

### Dynamic per-market query feeds

New `lib/market_feeds.py`:

```python
def build_query_feeds(markets: list[tuple[str, str]]) -> list[Feed]:
    """One Feed per (market_id, question) pair, querying Google News RSS
    search for that question's exact text. `market_id` is used only to build
    a distinct feed URL for conditional-GET caching — it is NOT threaded into
    the resulting Feed's name/category (see Metrics below)."""
```

- Feed URL: `https://news.google.com/rss/search?q=<urlencoded question>&hl=en-US&gl=US&ceid=US:en`.
- v1 query is the raw market question string, URL-encoded — no stopword
  stripping or entity extraction. Iterate on query construction later if match
  recall is poor in practice.
- Every generated `Feed` shares `name="Google News Query"`,
  `category="market_query"` — see Metrics section for why market_id doesn't
  flow into the Feed's identity.

### Syncer publishes the open-market snapshot to Redis

The open-market set changes **only** when the syncer runs — it is the sole
writer of the `markets` table (inserts new markets, marks resolved ones
closed), and does both in one cycle. So the syncer, not the feeder, is the
natural producer of the feed set, and it already holds a DB connection and
computes `db.corpus_counts()` each cycle.

New method on `lib/db.py`'s `Db`, alongside `open_market_ids()`:

```python
def open_market_questions(self) -> list[tuple[str, str]]:
    """(id, question) for every market WHERE NOT closed — same set as
    open_market_ids(), with the question text needed to build a query feed."""
```

At the end of each `sync_once` cycle, the syncer writes the **full**
`open_market_questions()` snapshot to a single Redis key
(`MARKET_FEED_SNAPSHOT_KEY`, default `market_feed_snapshot`) as a JSON list of
`[id, question]` pairs — a full **overwrite**, not an incremental update.

Why a full-overwrite snapshot rather than a per-market feed store with explicit
deletes: closed markets simply aren't in the next snapshot, so there is no
"delete on close" step to get wrong and no way for the feed set to drift out of
sync with the `markets` table. This is the same principle as the existing
`WHERE NOT closed` retrieval query — absence is the delete.

This keeps the **feeder DB-free**: it already talks to Redis (queue + dedup),
so reading one more key adds no new dependency. The feeder never gains a
Postgres connection.

### Feeder: second poll loop

`services/feeder/main.py` gains a second, independent async loop alongside the
existing static-feed loop:

- New config `market_feed_poll_interval_seconds` (env `MARKET_FEED_POLL_INTERVAL_SECONDS`,
  default `900` — 15 min). Deliberately decoupled from `poll_interval_seconds`
  (60s default): fetching Google News RSS for potentially hundreds of markets
  every minute risks tripping Google's abuse prevention. 15 minutes keeps
  dynamic-feed traffic modest while still catching same-day news.
- Each tick: read the `MARKET_FEED_SNAPSHOT_KEY` snapshot from Redis, pass the
  `(id, question)` pairs through `build_query_feeds`, then fetch each resulting
  feed through the existing `fetch_feed` / freshness-gate / dedup / queue-push
  path — unchanged from how static feeds are handled today.
- Staleness is bounded and harmless: because the snapshot only refreshes when
  the syncer runs (daily by default), a market that closes mid-day keeps being
  queried until the next snapshot. That produces only a few wasted
  (conditional-GET, mostly-304) fetches — a closed market can't receive a
  belief update anyway, since `top_k_markets` filters `WHERE NOT closed`.
- On a missing/empty/unparseable snapshot key (e.g. syncer hasn't run yet, or
  Redis hiccup): log a warning, skip the dynamic fetch this tick, and leave the
  static loop untouched. Never crash either loop.

### Retrieval stays unchanged: article-as-unit + top_k

Dynamic-feed articles flow through the **same** worker pipeline as static-feed
articles — embed → `top_k_markets` retrieval → per-candidate Groq relevance →
Claude reeval. No per-market targeting, no `market_id` threaded from feed to
worker.

This is deliberate, and it's what makes the design robust to the same story
appearing across many per-market feeds: the existing URL/guid dedup (7-day TTL)
collapses those duplicates to **one** queue entry (first feed wins; the rest
are 304s or deduped), so there is exactly one embed + one top_k + one set of
Groq calls per *unique* article regardless of how many feeds carried it. Feed
overlap is absorbed at the feeder's cheap HTTP layer and never reaches the
token-spending layer.

Per-market targeting (skip top_k, relevance-check only the originating market)
was considered and rejected: it fights dedup. Dedup discards *which* feed
surfaced an article, so targeting would require either dropping dedup — fanning
one article into one work-unit per market, the actual token-explosion risk — or
carrying every candidate market_id on the deduped article and checking them all,
which is just top_k by another name. The uniform pipeline avoids both. The real
cost lever is Claude reevals (Groq at top_k is the cheap gate by design); if a
news-storm safety valve is ever needed, a global per-cycle dispatch cap is the
right tool, independent of feed design — out of scope here.

### Metrics

Dynamic-feed articles must not create per-market Prometheus label series —
with potentially hundreds of open markets changing over time, per-market
labels are unbounded cardinality. Downstream matching (the worker's embedding
+ `top_k_markets` + Groq relevance check) re-derives candidate markets from
the article's content independently anyway — it never needs to know which
market's query surfaced a given article. So every dynamic-feed `Feed` shares
one `name`/`category`, and `FEEDER_ARTICLES_FETCHED{source="Google News Query"}` /
`FEEDER_ARTICLES_PUSHED{source="Google News Query"}` aggregate across all of
them. A new gauge, `FEEDER_MARKET_QUERY_FEEDS`, tracks how many dynamic feeds
are active after each refresh tick (visibility into whether the snapshot is
being read and how it scales with open-market count).

### Config summary

- `MARKET_FEED_POLL_INTERVAL_SECONDS` (default `900`) — feeder's dynamic-loop
  interval.
- `MARKET_FEED_SNAPSHOT_KEY` (default `market_feed_snapshot`) — Redis key the
  syncer writes and the feeder reads.

## Testing

- **Unit tests** for `market_feeds.build_query_feeds`: URL construction and
  encoding (spaces, punctuation, quotes in question text), empty input.
- **Unit test** for the syncer snapshot write: after `sync_once`, assert the
  Redis key holds the current `open_market_questions()` set as JSON, and that a
  subsequent cycle with a now-closed market overwrites it (closed market
  absent) — verifying implicit deletion.
- **Unit test** for the feeder's dynamic loop: given a snapshot in Redis,
  assert `fetch_feed` is called once per generated feed and results flow
  through dedup/queue as normal; separately, assert a missing/unparseable
  snapshot key logs a warning and does not crash either loop.
- **DB integration test** for `Db.open_market_questions`: skipped unless
  `TEST_DATABASE_URL` is set, matching the existing convention (e.g.
  `Db.log_relevance_check`'s test).
