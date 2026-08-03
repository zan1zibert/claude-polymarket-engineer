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

### `Db.open_market_questions()`

New method on `lib/db.py`'s `Db`, alongside `open_market_ids()`:

```python
def open_market_questions(self) -> list[tuple[str, str]]:
    """(id, question) for every market WHERE NOT closed — same set as
    open_market_ids(), with the question text needed to build a query feed."""
```

### Feeder: second poll loop

`services/feeder/main.py` gains a second, independent async loop alongside the
existing static-feed loop:

- New config `market_feed_poll_interval_seconds` (env `MARKET_FEED_POLL_INTERVAL_SECONDS`,
  default `900` — 15 min). Deliberately decoupled from `poll_interval_seconds`
  (60s default): fetching Google News RSS for potentially hundreds of markets
  every minute risks tripping Google's abuse prevention. 15 minutes keeps
  dynamic-feed traffic modest while still catching same-day news.
- Each tick: call `Db.open_market_questions()`, pass the result through
  `build_query_feeds`, then fetch each resulting feed through the existing
  `fetch_feed` / freshness-gate / dedup / queue-push path — unchanged from how
  static feeds are handled today.
- This is a real scope change: the feeder currently has no DB dependency (a
  deliberate "dumb producer" design). It now opens a read-only `Db` connection
  purely to enumerate which markets are open. Accepted as the cost of solving
  the specificity gap.
- On `Db` connection/query failure during a refresh tick: log a warning, keep
  using the last successfully-built query-feed list (empty list if this is the
  very first tick and it fails). Never crash the loop or affect the static
  loop.

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
are active after each refresh tick (visibility into whether the DB read is
working and how it scales with open-market count).

## Testing

- **Unit tests** for `market_feeds.build_query_feeds`: URL construction and
  encoding (spaces, punctuation, quotes in question text), empty input.
- **Unit test** for the feeder's dynamic loop: mock `Db.open_market_questions`
  to return a market list, assert `fetch_feed` is called once per generated
  feed and results flow through dedup/queue as normal; separately, assert a DB
  failure during refresh logs a warning and does not crash the loop.
- **DB integration test** for `Db.open_market_questions`: skipped unless
  `TEST_DATABASE_URL` is set, matching the existing convention (e.g.
  `Db.log_relevance_check`'s test).
