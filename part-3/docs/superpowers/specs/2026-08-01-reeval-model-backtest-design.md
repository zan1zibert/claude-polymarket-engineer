# Re-evaluation Model Backtest Harness — Design

**Date:** 2026-08-01
**Status:** Approved design, pre-implementation
**Author:** Žan Žibert (with Claude)

## Problem

The Groq relevance gate sharply cut the volume of candidates reaching Claude's
belief re-evaluation step, so each surviving call is higher-signal and per-call
cost now matters far less. That opens the door to a more capable / thinking-
enabled re-eval config. But we currently **cannot tell which config forecasts
better**: `belief_updates` and `forecast_scores` record no model attribution,
and `forecast_scores` grades the *final belief per market* (the product of
several updates from several articles), so a Brier score can't be traced to a
single re-eval config.

The objective, chosen explicitly, is **forecast accuracy** — maximize Brier
skill vs the `seed_price` market baseline. This is empirically measurable
because the scorer already computes Brier / log-loss against resolved outcomes.

This project builds the **measurement first**: an offline replay/backtest that
scores candidate re-eval configs against resolved markets, so model/technique
selection is data-driven rather than faith-based.

## Goals

- Persist enough per-re-eval data to faithfully replay a re-evaluation offline.
- Make "how a re-eval is done" a single parametrized config, shared by
  production and the harness.
- Score any candidate config against resolved markets: Brier skill vs the
  `seed_price` baseline, log-loss, calibration, and parse-failure rate.
- Rank configs with confidence intervals so a "winner" is a real difference.

## Non-goals (v2 / explicitly out of scope)

- **Trajectory replay** — threading each config's own output as the next prior.
  v1 is **myopic per-sample scoring** (prior held fixed at the recorded value).
- **Post-hoc calibration** (isotonic/Platt) as its own lever.
- **Live market-split A/B** — measurement-first was chosen over ship-and-measure.
- **Auto-promotion** — a human reads the report and flips the production config.

## Decision rule (the selection criterion, operationalized)

A candidate config replaces the incumbent when its out-of-sample **Brier skill**
is higher with a bootstrap confidence interval that clears the incumbent's, and
it does **not** regress the parse-failure rate. Latency is a secondary tiebreak.

## Architecture

Three units, each independently testable:

### 1. Capture — `reeval_samples` table + worker instrumentation

New migration `db/migrations/0005_reeval_samples.sql`:

```sql
CREATE TABLE IF NOT EXISTS reeval_samples (
    id                 BIGSERIAL PRIMARY KEY,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id          TEXT NOT NULL REFERENCES markets(id),
    article_url        TEXT NOT NULL,
    article_title      TEXT NOT NULL,
    article_summary    TEXT NOT NULL DEFAULT '',   -- the input missing everywhere today
    market_question    TEXT NOT NULL,              -- SNAPSHOT: markets rows drift under the syncer
    market_description TEXT NOT NULL DEFAULT '',    -- SNAPSHOT
    prior              DOUBLE PRECISION,            -- previous_score; NULL on first eval
    produced_score     DOUBLE PRECISION,            -- what production returned; NULL on parse fail
    config             JSONB NOT NULL               -- production config tag (model, thinking, web_search, prompt_version)
);

CREATE INDEX IF NOT EXISTS reeval_samples_market_idx
    ON reeval_samples (market_id, ts DESC);
```

Worker change (`services/worker/main.py`, `process_article`): for each matched
market, after the `reevaluate()` call, write one `reeval_samples` row carrying
the article payload, the market question/description **as seen at eval time**,
the prior, the produced score (NULL if parse failed), and the production config.

- Capture happens for **every** re-eval that runs, including parse failures
  (we keep the input regardless — a failure is data about the config).
- The insert is wrapped in try/except: a capture failure logs and continues,
  it must **never** fail a belief update. Same fail-open discipline as the
  existing `db.log_relevance_check`.

DB access: add `db.insert_reeval_sample(...)` and
`db.labeled_reeval_samples(...)` (the join below) to `lib/db.py`.

### 2. Config abstraction — `lib/reeval.py`

Move the re-eval into `lib/reeval.py` as a pure function of its inputs, driven
by a config dataclass:

```python
@dataclass(frozen=True)
class ReevalConfig:
    model: str
    thinking: bool = False          # adaptive thinking on/off
    effort: str | None = None       # "low"|"medium"|"high"|"xhigh"|"max"; None omits
    web_search_max_uses: int = 0    # 0 disables the web_search tool
    prompt_version: str = "v1"      # selects prompt template variant
    ensemble_n: int = 1             # sample N times, aggregate (median) the probability
    structured_output: bool = False # guarantee JSON via output_config.format

def reevaluate(config: ReevalConfig, market: dict, prior, article: dict) -> dict:
    ...  # returns {probability, confidence, reasoning, usage, ...} or {error, ...}
```

- This subsumes the current `reevaluate(market, current_score, article, *, model,
  use_web_search)` in `lib/claude.py`. Production keeps identical behavior by
  passing a `ReevalConfig` built from current settings
  (`model=ANTHROPIC_MODEL`, `web_search_max_uses = 1 if worker_use_web_search
  else 0`, everything else default).
- New capabilities exposed for the harness to sweep, per the claude-api skill:
  - `thinking=True` sets `thinking={"type": "adaptive"}` (must be explicit —
    current models run thinking-off when omitted).
  - `effort` sets `output_config={"effort": ...}`.
  - `web_search_max_uses` uses the `web_search_20260209` tool.
  - `structured_output` uses `output_config.format` (JSON schema) so the
    probability always parses.
  - `ensemble_n > 1` runs N samples and aggregates (median probability).
- Config (de)serialization: `ReevalConfig.to_dict()` / `from_dict()` so it round-
  trips through the `reeval_samples.config` JSONB column and the harness's config
  file.

### 3. Backtest harness — `scripts/backtest_reeval.py` (offline CLI)

Reads a **config file** (chosen over a built-in list) listing the candidate
configs to sweep, plus dataset filters. JSON, to avoid adding an undeclared
PyYAML dependency (it's only transitively present today):

```json
// configs/backtest_reeval.json (example)
{
  "dataset": { "since": "2026-08-01", "min_samples": 30 },
  "candidates": [
    { "name": "incumbent", "model": "claude-sonnet-4-6" },
    { "name": "sonnet5-thinking", "model": "claude-sonnet-5",
      "thinking": true, "effort": "high" },
    { "name": "opus-thinking-search", "model": "claude-opus-4-8",
      "thinking": true, "effort": "high", "web_search_max_uses": 3 }
  ]
}
```

(`since` is an optional `ts` lower bound; `min_samples` is the floor below which
the harness refuses to report. Each candidate object is a `ReevalConfig` plus a
display `name`.)

Steps:

1. **Load labeled samples** — `db.labeled_reeval_samples()`:
   `reeval_samples ⋈ markets` where `markets.resolved_outcome IS NOT NULL`,
   returning per sample: article payload, market snapshot, `prior`,
   `resolved_outcome` (label), and `seed_price` (baseline). Apply dataset
   filters. Abort with a clear message if `< min_samples`.
2. **Replay** — for each candidate × each sample, call
   `reevaluate(config, market_snapshot, prior, article)`. Prior is the recorded
   value, so every candidate sees identical inputs. Concurrency-bounded (low
   volume; a modest worker pool). Cache `(config_hash, sample_id) → probability`
   on disk so re-runs don't re-pay for Claude / web-search calls.
3. **Score** — reuse `lib/scoring.py`:
   - per prediction: `brier(p, y)`, `log_loss(p, y)`;
   - per config: mean Brier, mean log-loss over parsed predictions;
   - baseline: same metrics computed on `seed_price` per market;
   - `skill_score(mean_brier_config, mean_brier_baseline)`;
   - calibration via `reliability_bins(...)`;
   - parse-failure rate = failed / total.
4. **Report** — a table ranked by Brier skill, each config with a **bootstrap
   CI** (resample samples with replacement, recompute skill, take percentiles),
   plus log-loss, calibration summary, parse-failure rate, and token/cost
   totals. Emit to stdout and a JSON/CSV artifact under `outputs/`.

The winner is read off the report by a human per the decision rule above.

## Data flow

```
worker.process_article
  └─ reevaluate(prod_config, market, prior, article)      # lib/reeval.py
  └─ db.insert_reeval_sample(...)  (fail-open)             # capture

... markets resolve; syncer backfills markets.resolved_outcome ...

backtest_reeval.py
  ├─ db.labeled_reeval_samples()      # reeval_samples ⋈ resolved markets
  ├─ for candidate in config_file:
  │     for sample: reevaluate(candidate, ...)  # cached
  ├─ lib/scoring.py: brier / log_loss / skill_score / reliability_bins
  └─ ranked report + CIs → stdout + outputs/backtest_*.json
```

## Error handling

- **Capture** never raises into the worker's belief path (try/except + log).
- **Harness** per-sample failures (Claude/Groq/web-search errors, parse fail)
  are counted in the parse-failure metric and excluded from Brier/log-loss
  means — not silently dropped.
- **Log-loss** clamps probabilities via `scoring.clamp01(p, eps)` (already the
  module's convention) to keep `ln()` finite.
- **Empty / too-small dataset** → the harness aborts with a message naming the
  labeled-sample count, rather than reporting a meaningless skill number.

## Testing

- **Unit** — `ReevalConfig` round-trip (dataclass ↔ dict ↔ JSONB); the request
  kwargs each config field produces (thinking/effort/web_search/structured);
  `db.insert_reeval_sample` / `labeled_reeval_samples` against a test DB.
- **Golden** — a fixture set of labeled samples with a **stubbed** `reevaluate`
  returning fixed probabilities, asserting exact Brier / log-loss / skill /
  calibration / parse-failure outputs. Exercises aggregation + reporting with
  no API calls.
- **Capture path** — worker writes one sample per matched market; the insert-
  failure path logs and does not raise.
- **No live-API test in CI** — the harness's real Claude calls are manual /
  ad-hoc, gated on credentials.

## Rollout / expectations

- Ship capture + config abstraction first; production behavior is unchanged
  (incumbent config passed through the new abstraction).
- Capture-from-now means the first real backtest numbers arrive only once a
  minimum set of sampled markets resolves (days–weeks). The harness itself is
  validated immediately via the golden fixtures.
- Once enough labeled samples exist, run the sweep, read the report, and flip
  the production `ReevalConfig` (env/config) to the winner if it clears the
  decision rule.

## Files touched

- `db/migrations/0005_reeval_samples.sql` — new table.
- `lib/reeval.py` — new; `ReevalConfig` + `reevaluate()` (absorbs `lib/claude.py`
  re-eval logic).
- `lib/claude.py` — reduced to the Anthropic client / parsing helpers reused by
  `lib/reeval.py`, or folded into it.
- `lib/db.py` — `insert_reeval_sample`, `labeled_reeval_samples`.
- `services/worker/main.py` — build a `ReevalConfig` from settings; capture a
  sample per matched market (fail-open).
- `scripts/backtest_reeval.py` — new; the offline harness.
- `configs/backtest_reeval.json` — example candidate sweep.
- Tests under the repo's existing test layout.
```
