# Implementation Plan — Priority 1: Close the Loop (ground truth → scoring → calibration → backtest)

## Context

The full architecture review ranked six improvement areas. The chosen lens is
**forecasting edge / quant rigor** with **real capital eventually**. This document details
**Priority 1**, the gating item: the system today is an **open loop** — it mutates
`current_score` but never learns whether its probabilities were right, and it cannot,
because:

- **Outcomes are discarded.** `db.mark_resolved` (`lib/db.py:170`) sets only `closed=TRUE`
  + `resolved_at`; the winning YES/NO outcome is never stored. `polymarket.fetch_statuses`
  (`lib/polymarket.py:106`) returns only `{closed, end_date}`.
- **The market baseline is fragile.** The Polymarket price at ingest survives only
  implicitly as the *first* `belief_updates.previous_score` for a market — lost if the
  seed logic changes, and awkward to query.
- **Nothing scores anything.** No Brier / log-loss, no calibration, no per-feed edge.

Nothing else in the roadmap can be validated until this exists. This plan adds
ground-truth capture, a scoring service, calibration, dashboards, and a backtest harness.

Scope note: Parts A–D are the essential "closed loop." Part E (backtest) depends on
price-history capture (Part A2) and is the natural follow-on within the same priority.

---

## Part A — Capture ground truth (the prerequisite)

### A1. Store the winning outcome and the market baseline

**`db/init.sql`** — add columns (with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for
forward-compat, matching the existing pattern at lines 36–40):
- `markets.resolved_outcome DOUBLE PRECISION` — NULL until known; `1.0` if YES won, `0.0`
  if NO won. NULL means "closed but outcome not determinable" (cancelled/delisted).
- `markets.seed_price DOUBLE PRECISION` — the Polymarket YES price at ingest (the market
  baseline for scoring). Set explicitly at insert so it's unambiguous and survives.
- Partial index `CREATE INDEX ... ON markets (id) WHERE closed AND resolved_outcome IS NULL`
  — the scorer/backfill set.

**`lib/polymarket.py`** — teach the client to read a resolved outcome:
- Add `resolved_yes_price(market: dict) -> Optional[float]`: parse `outcomePrices`; for a
  settled binary market Gamma returns `["1","0"]`/`["0","1"]`, so return the YES leg. Only
  return a value when it's within ~1e-3 of 0 or 1 (definitive); otherwise `None`.
- Extend `fetch_statuses` to include `yes_price` (parsed from `outcomePrices`) alongside
  `{closed, end_date}`, so the syncer can derive the outcome without a second call.
- In `normalize` (`lib/polymarket.py:35`), surface `yes_price` as the seed (already
  computed at line 56) — thread it through `insert_markets` as `seed_price`.

**`lib/db.py`**:
- `insert_markets` (`lib/db.py:128`): add `seed_price` to the INSERT (source:
  `m["yes_price"]`, the same value currently used for `current_score`).
- Replace `mark_resolved(ids)` with `mark_resolved(outcomes: dict[str, Optional[float]])`
  — set `closed=TRUE, resolved_at=now(), resolved_outcome=%s` per id. Keep it idempotent
  (`WHERE ... AND NOT closed`), and return the count newly closed.
- Add `backfill_outcomes(outcomes: dict[str, float]) -> int`: `UPDATE ... SET
  resolved_outcome=%s WHERE id=%s AND closed AND resolved_outcome IS NULL` — fills in
  outcomes that settled *after* we closed the market.
- Add `markets_awaiting_outcome() -> list[str]`: `SELECT id FROM markets WHERE closed AND
  resolved_outcome IS NULL` — the backfill set.

### A2. Capture a market-price time series (needed for baseline-over-time + backtest)

Today we store only the ingest price and the current belief — no price history. Add a
lightweight time series so the syncer's periodic price observations accumulate:

**`db/init.sql`** — new table:
```sql
CREATE TABLE IF NOT EXISTS market_prices (
    market_id  TEXT NOT NULL REFERENCES markets(id),
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    yes_price  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (market_id, ts)
);
```
**`lib/db.py`** — `record_prices(rows: list[tuple[str, float]])` bulk insert.
**`services/syncer/main.py`** — in `sync_once`, after fetching statuses, record the current
`yes_price` for every open market. (Optional stronger version: backfill history from the
Polymarket CLOB `/prices-history` endpoint — note it, defer it.)

### A3. Wire outcome capture into the syncer

**`services/syncer/main.py`** (`sync_once`, lines 97–104):
- Build `to_resolve` as today, but compute an outcome map:
  `{id: polymarket.resolved_yes_price(statuses.get(id))}` (rounded to 0/1, or None).
- Call the new `mark_resolved(outcomes)`.
- Add a second pass: `ids = db.markets_awaiting_outcome()`, re-query Gamma statuses for
  them, `db.backfill_outcomes(...)`. This is the fix for "market settles after we closed
  it" — required for scoring completeness.
- Add metric `SYNCER_OUTCOMES_BACKFILLED` (Counter).

### A4. Attribute forecasts to feeds (so "which feeds add edge" is answerable)

`belief_updates` stores `article_url` but not `source`; the worker's per-source metrics
can't be joined to resolved outcomes. Add:
- **`db/init.sql`**: `ALTER TABLE belief_updates ADD COLUMN IF NOT EXISTS source TEXT;`
- **`lib/schemas.py`**: add `source: Optional[str]` to `BeliefUpdate`.
- **`lib/db.py`** `apply_belief_update`: accept + insert `source`.
- **`services/worker/main.py`** (`process_article`, line 104): pass `article.source`.

---

## Part B — The scorer service

A new singleton service (mirrors the syncer's shape: periodic loop, `--once` flag, metrics
server, graceful shutdown).

### B1. Pure scoring functions — `lib/scoring.py` (new)
- `brier(p: float, y: float) -> float` = `(p - y) ** 2`.
- `log_loss(p, y, eps=1e-15) -> float` with clamping.
- `clamp01(p)`, and a `reliability_bins(pairs, n_bins=10)` returning per-bin
  (mean_predicted, observed_frequency, count) for calibration curves.
- Kept dependency-free and pure → unit-testable in isolation (feeds Priority 5's test
  suite).

### B2. Scores table — `db/init.sql` (new table)
```sql
CREATE TABLE IF NOT EXISTS forecast_scores (
    market_id       TEXT PRIMARY KEY REFERENCES markets(id),
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome         DOUBLE PRECISION NOT NULL,      -- 0/1
    final_belief    DOUBLE PRECISION NOT NULL,      -- our last current_score
    seed_price      DOUBLE PRECISION,               -- market baseline at ingest
    n_updates       INT NOT NULL,
    brier_belief    DOUBLE PRECISION NOT NULL,
    logloss_belief  DOUBLE PRECISION NOT NULL,
    brier_baseline  DOUBLE PRECISION,               -- market-at-ingest baseline
    logloss_baseline DOUBLE PRECISION
);
```
**`lib/db.py`** additions: `resolved_unscored_markets()` (join `markets` with
`resolved_outcome NOT NULL` and no `forecast_scores` row, returning
`current_score, seed_price` and `count(belief_updates)`), and `insert_score(...)`.

### B3. Service loop — `services/scorer/main.py` (new)
Each cycle: pull `resolved_unscored_markets()`; for each, compute Brier + log-loss for our
`final_belief` and for the `seed_price` baseline against `outcome`; `insert_score(...)`.
Update rolling gauges (Part B4). Config knob `SCORER_INTERVAL_SECONDS` (default e.g.
3600). Register in `Dockerfile` (new `scorer` stage) and `docker-compose.yml` (+
`docker-compose.prod.yml`), same pattern as the syncer.

### B4. Metrics — `lib/metrics.py`
- `SCORER_MARKETS_SCORED` (Counter).
- Gauges refreshed each cycle from aggregate SQL: `forecast_brier_mean`,
  `forecast_logloss_mean`, and the same for the baseline, plus
  `forecast_brier_skill` (1 − brier_belief/brier_baseline — the headline "are we beating
  the market?" number). Optionally labelled by feed `source` via the Part A4 join.

---

## Part C — Calibration

- **`lib/scoring.py`**: `reliability_bins` (above) + `fit_isotonic(pairs)` returning a
  monotonic mapping (via `scikit-learn` `IsotonicRegression`; add `scikit-learn` to
  `part-3/requirements.txt`).
- **New table** `calibration_bins (bin, mean_predicted, observed_freq, count, computed_at)`
  written by the scorer each cycle over all scored forecasts → Grafana reads it.
- **Post-hoc calibrator artifact**: scorer periodically fits isotonic on
  `(final_belief, outcome)` pairs and persists knots to a small `calibrators` table (or a
  volume-mounted file). *Applied later by the signal service (Priority 3)* to map raw
  Claude probabilities → calibrated ones; capturing it here is what makes that possible.

---

## Part D — Dashboards (`monitoring/grafana/dashboards/`)

Add panels to the existing pipeline dashboard (or a new `accuracy.json`):
- Brier / log-loss over time (belief vs. baseline) — from the new gauges.
- **Brier skill score** single-stat (the "beating the market?" headline).
- Calibration/reliability diagram from `calibration_bins`.
- Per-feed edge table (brier_belief vs brier_baseline grouped by `source`).
Prometheus already scrapes any new service on `:8000`; the scorer only needs adding to
`monitoring/prometheus.yml` static targets if not using DNS SD.

---

## Part E — Backtest harness (follow-on within P1; depends on A2)

A standalone module/CLI (`services/backtest/` or `tools/backtest.py`), **not** a running
service:
- Replays `belief_updates` for resolved markets in `ts` order with strict **point-in-time**
  discipline: at each update time `t`, the market price is `market_prices` as-of `t`
  (never later), and only outcomes known after resolution are used for scoring.
- Simulates a simple strategy (edge = belief − price beyond a threshold) and reports Brier
  improvement, hypothetical PnL **net of a configurable fee/slippage**, and calibration on
  a held-out time split.
- Explicitly guards against look-ahead / survivorship (see López de Prado). Output: a
  summary table + optional matplotlib plot; no orders, no live calls.

---

## Config additions (`lib/config.py`)
Add to `Settings` + `load_settings`, following the existing env-var pattern:
`scorer_interval_seconds` (`SCORER_INTERVAL_SECONDS`, default 3600), and any backtest
knobs (fee/slippage, edge threshold) read by the CLI. Document defaults inline as the file
already does.

## Files touched (summary)
- **Modify:** `db/init.sql`, `lib/polymarket.py`, `lib/db.py`, `lib/schemas.py`,
  `lib/config.py`, `lib/metrics.py`, `services/syncer/main.py`,
  `services/worker/main.py`, `Dockerfile`, `docker-compose.yml`,
  `docker-compose.prod.yml`, `monitoring/prometheus.yml`, `part-3/requirements.txt`.
- **Add:** `lib/scoring.py`, `services/scorer/main.py`,
  `services/backtest/` (or `tools/backtest.py`),
  `monitoring/grafana/dashboards/accuracy.json`.

## Verification
1. **Unit** (new, seeds Priority 5): `lib/scoring.py` — Brier/log-loss/reliability on known
   inputs; `polymarket.resolved_yes_price` on sample resolved payloads (YES-won, NO-won,
   ambiguous → None).
2. **Migration**: bring up the stack; confirm `ALTER TABLE`s apply idempotently on the
   existing volume and new columns/tables exist.
3. **Ground truth**: run `python -m services.syncer.main --once` against a window
   containing at least one recently-resolved market; assert `resolved_outcome` and
   `seed_price` populate, and that a market closed-by-end_date then settled later gets
   backfilled on a second run.
4. **Scoring**: run the scorer once; assert `forecast_scores` rows appear with sane Brier
   (0 perfect, 0.25 for a 0.5 guess) and that `forecast_brier_skill` computes.
5. **Dashboards**: load Grafana; confirm the accuracy panels + calibration diagram render
   from real rows.
6. **Backtest**: run the CLI over accumulated data; sanity-check that a no-edge strategy
   nets ≤ 0 after fees and the point-in-time price lookup never reads a future timestamp.

---

## Where this fits (the six-priority roadmap)

1. **Close the loop** (this doc) — ground truth, scoring, calibration, backtest.
2. **Principled belief model** — update in log-odds space, aggregate multiple signals,
   time decay, use the `confidence` field (currently discarded at
   `services/worker/main.py:102-103`), switch to structured output instead of the
   defensive JSON scrape in `lib/claude.py:104-109`.
3. **Signal service + risk/execution** — edge vs. live CLOB price, fractional-Kelly
   sizing, fees/slippage; applies P1's calibrator; paper-trade first.
4. **Retrieval quality** — hybrid (dense + BM25) + cross-encoder rerank, entity/temporal
   grounding, instead of the bare cosine cutoff in `lib/db.py` `top_k_markets`.
5. **Reliability** — at-least-once delivery (Redis Streams), retries/backoff/circuit
   breakers, idempotency, and the missing tests + CI + dependency pinning.
6. **Cost & scale** — model cascade (Haiku triage → Sonnet), prompt caching, Batch API,
   async within the worker.

Once P1 lands, every later item is judged by whether Brier/calibration improve and paper
PnL (net of fees) is positive on a look-ahead-free backtest.

## Key reading
- Halawi et al., *Approaching Human-Level Forecasting with Language Models* (2024) — nearly
  this exact architecture, done rigorously.
- López de Prado, *Advances in Financial Machine Learning* — backtesting pitfalls.
- Gneiting & Raftery, *Strictly Proper Scoring Rules, Prediction, and Estimation* (2007).
- Guo et al., *On Calibration of Modern Neural Networks* (2017).
- Kleppmann, *Designing Data-Intensive Applications* — delivery semantics/idempotency.
- Tetlock & Gardner, *Superforecasting*.
