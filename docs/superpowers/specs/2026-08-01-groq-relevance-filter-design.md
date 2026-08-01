# Worker: Groq Relevance Filter — Design

**Date:** 2026-08-01
**Status:** Approved (pending implementation plan)

## Problem

The worker's relevance gate is a raw pgvector cosine-distance threshold
(`max_cosine_distance`, default `0.35`). Embedding distance catches
topical/vocabulary proximity, not event identity: an article and a market can
share entities or keywords while describing genuinely unrelated events (e.g.
both mention "the Fed," one about a nominee, the other about a rate-cut
market). These false-positive candidates pass the gate, get sent to Claude for
a price-blind re-evaluation, and Claude correctly reports they're unrelated —
wasting a Claude call and adding noise around what should be a clean
belief-update stream.

This is the first step of a larger pipeline redesign (which will later add an
X/Twitter ingestion source and time-bucketed batching). Scoped narrowly here:
fix relevance filtering on the existing RSS pipeline before touching data
sources.

## Goal

Replace the cosine-distance threshold with an actual relevance judgment.
pgvector keeps doing retrieval (find candidate markets at all); a cheap,
fast LLM call on Groq decides whether each candidate is genuinely about the
same real-world event as the article, before Claude ever sees it.

## Non-goals

- No X/Twitter ingestion, no time-bucketing/batching of belief updates — those
  are separate, later specs.
- No change to `reevaluate()`'s Claude prompt/behavior — Claude still gets one
  article + one market per call, price-blind, exactly as today.
- No change to the feeder, syncer, or scorer services.
- No widening beyond `top_k = 10` in this pass — validate the Groq filter's
  quality at this width before considering going wider.

## Design

### Retrieval vs. filtering split

`db.top_k_markets(embedding, top_k)` becomes retrieval-only: it returns the
`top_k` nearest markets by embedding distance, with no distance cutoff.
`max_cosine_distance` is removed from `Settings` and the worker entirely.
`top_k` default moves from `5` to `10` (still overridable via the `TOP_K` env
var), since the relevance decision no longer has to lean on retrieval width to
mask a weak filter.

### Groq relevance check

New `lib/groq_relevance.py`, mirroring the structure of `lib/claude.py`:

```python
def check_relevance(article: dict, market: dict, *, model: str) -> dict:
    """Returns {"relevant": bool, "reasoning": str} or {"error": ..., "raw": ...}."""
```

- `article` needs `{title, summary, url}`; `market` needs `{question,
  description}` — same shapes already used by `reevaluate()`.
- Uses the Groq API (`groq` Python client) with `llama-3.1-8b-instant`
  (configurable, see below).
- New prompt files under `prompts/`: `relevance_system_prompt.txt` and
  `relevance_prompt.txt`. The prompt asks: does this article describe the
  same real-world event/development that this market's question is about?
  Answer strictly on event identity, not topical similarity.
- Same defensive JSON-extraction/parsing approach as `lib/claude.py`'s
  `_final_text` / parse path, for consistency.

### Worker integration

In `services/worker/main.py`, `process_article`:

1. Embed the article (unchanged).
2. `markets = db.top_k_markets(embedding, settings.top_k)` — retrieval only,
   no threshold.
3. For each candidate market:
   - Call `check_relevance(article_payload, market_payload, model=settings.groq_model)`.
   - Log the outcome via `db.log_relevance_check(...)` (see schema below),
     regardless of verdict.
   - If `relevant` is not `True` (includes explicit `False` and any error/
     timeout — fail closed), skip this candidate and continue to the next.
   - If `relevant is True`, proceed to `reevaluate()` exactly as today.
4. Everything downstream of a passing candidate (belief update, queue push,
   audit log, metrics) is unchanged.

A Groq API error or timeout for a candidate is treated as **not relevant**
(fail closed): logged to `relevance_checks` with `relevant = false` and
`reasoning = "groq_error: <message>"`, distinguishing failures from genuine
negative verdicts in the data without changing worker control flow.

### Schema: `relevance_checks` table

New migration `db/migrations/0004_relevance_checks.sql`:

```sql
CREATE TABLE relevance_checks (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    article_url  TEXT NOT NULL,
    article_title TEXT NOT NULL,
    market_id    TEXT NOT NULL REFERENCES markets(id),
    relevant     BOOLEAN NOT NULL,
    reasoning    TEXT NOT NULL,
    model        TEXT NOT NULL
);

CREATE INDEX idx_relevance_checks_market_id ON relevance_checks(market_id);
```

`market_id` is a foreign key into `markets` — every row originates from a
`top_k_markets` candidate, so the referenced market always exists.

`lib/db.py` gets:

```python
def log_relevance_check(self, article: Article, market: Market, relevant: bool, reasoning: str, model: str) -> None
```

This replaces the JSONL-audit-log approach used elsewhere in the worker
(`_audit` / `audit_log_path`) for this specific data — relevance checks are
structured, queryable, and joined against `markets`, which a flat file isn't
well suited for.

### Config (`lib/config.py`)

- `groq_api_key: str` — from `GROQ_API_KEY` env var.
- `groq_model: str` — from `GROQ_MODEL`, default `"llama-3.1-8b-instant"`.
- `top_k` default: `5` → `10`.
- Removed: `max_cosine_distance` / `MAX_COSINE_DISTANCE`.

### Metrics (`lib/metrics.py`)

Three new counters, labeled by `source` (matching existing `WORKER_*` metrics):

- `WORKER_GROQ_RELEVANT` — candidate passed the relevance check.
- `WORKER_GROQ_REJECTED` — candidate failed the relevance check (genuine
  negative verdict).
- `WORKER_GROQ_FAILURES` — Groq API error/timeout (counted separately from
  `WORKER_GROQ_REJECTED` even though both fail closed, so outages are visible).

### Error handling

- Groq API errors/timeouts: fail closed (see above), counted in
  `WORKER_GROQ_FAILURES`, logged to `relevance_checks`.
- Malformed/unparseable Groq JSON response: treated the same as an API error
  (fail closed, logged, counted as a failure) rather than crashing the worker
  loop.
- `db.log_relevance_check` failures (e.g. transient DB issue): logged as a
  warning; does not block the worker from moving to the next candidate (the
  relevance decision already made still governs whether `reevaluate()` runs).

## Testing

- **Unit tests** for `lib/groq_relevance.check_relevance`: JSON parsing
  (valid response, malformed response, missing fields) — mocking the Groq
  client, mirroring however `lib/claude.py` is currently tested.
- **Unit test** for `process_article`: mock `check_relevance` to return mixed
  relevant/not-relevant verdicts across multiple candidates, assert
  `reevaluate()` is only called for the relevant ones, and
  `log_relevance_check` is called once per candidate regardless of verdict.
- **DB integration test** for `db.log_relevance_check` and the
  `relevance_checks` migration: skipped unless `TEST_DATABASE_URL` is set,
  matching the existing convention (`pytest` stays green with no
  infrastructure).
