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
- Produce **belief-convergence graphs** per market — the candidate's belief
  trajectory over the market's update sequence, walking toward/away from the
  resolved outcome (requires trajectory replay; see below).
- Rank configs with confidence intervals so a "winner" is a real difference.

v1 runs **both** scorings (they answer different questions and share one sample
load): **myopic** per-sample (prior held fixed at the recorded value → clean
identical-input ranking) and **trajectory** replay (thread the candidate's own
output as the next prior → convergence graphs + final-belief skill).

## Non-goals (v2 / explicitly out of scope)

- **Measuring the web-search lever** — search cannot be faithfully backtested
  offline (see "Temporal faithfulness" below), so the harness disables it and
  its value is measured live in the market-split A/B instead.
- **Faithful point-in-time retrieval** — a custom, date-bounded retrieval tool
  over a time-stamped index (the only way to backtest search without lookahead).
- **Post-hoc calibration** (isotonic/Platt) as its own lever.
- **Live market-split A/B** — measurement-first was chosen over ship-and-measure.
- **Auto-promotion** — a human reads the report and flips the production config.

## Decision rule (the selection criterion, operationalized)

A candidate config replaces the incumbent when its out-of-sample **Brier skill**
is higher with a bootstrap confidence interval that clears the incumbent's, and
it does **not** regress the parse-failure rate. The primary metric is **myopic**
Brier skill (highest statistical power — every sample is an independent data
point); **final-belief** (trajectory) skill and the convergence graphs are
corroborating evidence, not the primary gate, since they have far fewer
independent units (one per market). Latency is a secondary tiebreak.

## Architecture

Three units, each independently testable:

### 1. Capture — `reeval_samples` table + worker instrumentation

New migration `db/migrations/0005_reeval_samples.sql`. A config is **normalized
into its own table** (a `ReevalConfig` blob is identical across the ~100 samples
a config produces — storing it per row is wasteful), with samples FK-ing to it:

```sql
-- Registry of every distinct config ever run (production capture AND backtest
-- candidates). config_hash is a content hash of the canonical config JSON, so
-- get-or-create dedups: the same config maps to one row.
CREATE TABLE IF NOT EXISTS reeval_configs (
    id          BIGSERIAL PRIMARY KEY,
    config_hash TEXT NOT NULL UNIQUE,        -- sha256 of canonical ReevalConfig JSON
    config      JSONB NOT NULL,              -- model, thinking, effort, web_search, prompt_version, ...
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reeval_samples (
    id                 BIGSERIAL PRIMARY KEY,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id          TEXT NOT NULL REFERENCES markets(id),
    config_id          BIGINT NOT NULL REFERENCES reeval_configs(id),  -- FK, not a per-row blob
    article_url        TEXT NOT NULL,
    article_title      TEXT NOT NULL,
    article_summary    TEXT NOT NULL DEFAULT '',   -- the input missing everywhere today
    market_question    TEXT NOT NULL,              -- SNAPSHOT: markets rows drift under the syncer
    market_description TEXT NOT NULL DEFAULT '',    -- SNAPSHOT
    prior              DOUBLE PRECISION,            -- previous_score; NULL on first eval
    produced_score     DOUBLE PRECISION             -- what production returned; NULL on parse fail
);

CREATE INDEX IF NOT EXISTS reeval_samples_market_idx
    ON reeval_samples (market_id, ts);   -- ASC: trajectory replay walks the sequence in order
```

Worker change (`services/worker/main.py`, `process_article`): for each matched
market, after the `reevaluate()` call, get-or-create the production config row
(`db.get_or_create_reeval_config`) and write one `reeval_samples` row referencing
it, carrying the article payload, the market question/description **as seen at
eval time**, the prior, and the produced score (NULL if parse failed).

- Capture happens for **every** re-eval that runs, including parse failures
  (we keep the input regardless — a failure is data about the config).
- The insert is wrapped in try/except: a capture failure logs and continues,
  it must **never** fail a belief update. Same fail-open discipline as the
  existing `db.log_relevance_check`.

DB access: add `db.get_or_create_reeval_config(config)`, `db.insert_reeval_sample(...)`,
and `db.labeled_reeval_samples(...)` (the join below) to `lib/db.py`.

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
- Config (de)serialization: `ReevalConfig.to_dict()` / `from_dict()` plus a
  `config_hash` property (sha256 of the canonical, key-sorted JSON) so a config
  round-trips through the `reeval_configs` registry, the harness's config file,
  and the backtest cache key. The canonical form makes the hash stable regardless
  of field order.

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

**The harness force-disables web search (and any live-retrieval tool) for every
candidate** — `web_search_max_uses` is zeroed regardless of the config file, and
the override is logged. This is not a preference; it is required for a sound
offline result (see "Temporal faithfulness" below).

Steps:

1. **Load labeled samples** — `db.labeled_reeval_samples()`:
   `reeval_samples ⋈ reeval_configs ⋈ markets` where
   `markets.resolved_outcome IS NOT NULL`, returning per sample: article payload,
   market snapshot, `prior`, `ts`, `resolved_outcome` (label), and `seed_price`
   (baseline). Apply dataset filters. Abort with a clear message if
   `< min_samples`.
2. **Replay** — two modes, both run from the same loaded samples:
   - **Myopic** — for each candidate × sample, call `reevaluate(config,
     market_snapshot, prior, article)` with the **recorded** prior, so every
     candidate sees identical inputs. Cache key `(config_hash, sample_id)`.
   - **Trajectory** — group samples by `market_id`, order by `ts`, and replay the
     sequence threading the candidate's **own** output as the next prior; anchor
     the first step at that market's first recorded prior (typically
     `seed_price`). Sequential within a market, parallel across markets. Cache
     key `(config_hash, market_id, step_index)`. Note: only the re-eval step
     varies — the article set per market is fixed (retrieval + Groq gate are
     upstream and held constant).

   Concurrency-bounded (low volume; a modest worker pool). The disk cache lets
   re-runs skip already-computed Claude calls.
3. **Score** — reuse `lib/scoring.py`:
   - myopic: `brier`/`log_loss` per prediction vs `resolved_outcome`;
   - trajectory: score each config's **final** belief per market, plus every
     intermediate step (for the convergence series);
   - per config: mean Brier, mean log-loss; baseline metrics from `seed_price`;
   - `skill_score(mean_brier_config, mean_brier_baseline)` for both myopic and
     final-belief;
   - calibration via `reliability_bins(...)`; parse-failure rate = failed/total.
4. **Report** — a table ranked by Brier skill (myopic + final-belief), each config
   with a **bootstrap CI** (resample with replacement, recompute skill, take
   percentiles), plus log-loss, calibration summary, parse-failure rate, and
   token/cost totals. Plus **convergence data**: per-market (and averaged)
   belief-vs-step series per config, with the resolved outcome as the reference.
   Emit the table to stdout and the trajectory series as a JSON/CSV artifact
   under `outputs/` — this is the always-available output and needs no extra
   dependency. Rendering those series to PNG **convergence graphs** uses
   matplotlib, which is **not currently a dependency**: add it as an optional
   extra and gate the plotting step so the harness still runs and writes the
   series when it's absent (log a note instead of failing).

The winner is read off the report by a human per the decision rule above.

## Temporal faithfulness (no lookahead)

The backtest runs *now*, after markets have resolved. Any candidate ability to
consult the present-day web is **outcome leakage** — it would let the model read
the answer and make search-enabled configs look brilliant for the wrong reason.

- **Blocking `polymarket.com` is not enough.** The resolution is reported by
  countless outlets; a domain blocklist can't enumerate every leaking source.
- **The correct rule is point-in-time**: search should only see the web as it
  existed when the article was published. But the built-in `web_search_20260209`
  tool exposes only `max_uses` / `allowed_domains` / `blocked_domains` /
  `user_location` — **no `before:` / date-range filter**. There is no way to
  constrain it to a historical cutoff. So faithful point-in-time search is *not
  achievable* with the tools we have.

**Consequence — the offline harness disables web search (and any live tool) for
every candidate.** It therefore measures only the contamination-free levers:
model tier, thinking, effort, prompt variant, ensemble. The **value of web
search is measured live** in the market-split A/B (v2), where the search happens
at genuine point-in-time. Faithful offline search would require a custom,
date-bounded retrieval tool over a time-stamped index — explicitly v2+.

**Residual caveat we can't eliminate: parametric leakage.** If a candidate
model's training cutoff postdates a market's resolution, it may already *know*
the outcome with no tool at all. Mitigations: prefer backtesting on markets that
resolved **after** the candidate's training cutoff, and treat any remaining
leakage as a bounded, roughly config-common effect. The harness records each
market's `resolved_at` so a cutoff filter can be applied per candidate.

## Data flow

```
worker.process_article
  └─ reevaluate(prod_config, market, prior, article)        # lib/reeval.py
  └─ db.get_or_create_reeval_config(prod_config) → id       # normalized registry
  └─ db.insert_reeval_sample(config_id=id, ...) (fail-open) # capture

... markets resolve; syncer backfills markets.resolved_outcome ...

backtest_reeval.py            # web search force-disabled for all candidates
  ├─ db.labeled_reeval_samples()   # reeval_samples ⋈ reeval_configs ⋈ resolved markets
  ├─ for candidate in config_file:
  │     myopic:     for sample:  reevaluate(candidate, recorded_prior, ...)   # cached
  │     trajectory: per market:  thread candidate output as next prior        # cached
  ├─ lib/scoring.py: brier / log_loss / skill_score / reliability_bins
  └─ ranked report + CIs → stdout; trajectory series → outputs/backtest_*.json
     (+ optional PNG convergence graphs if matplotlib present)
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

- **Unit** — `ReevalConfig` round-trip (dataclass ↔ dict ↔ JSONB) and a stable
  `config_hash` under field reordering; the request kwargs each config field
  produces (thinking/effort/web_search/structured); `get_or_create_reeval_config`
  dedups (same config → one row, second call returns the same id);
  `insert_reeval_sample` / `labeled_reeval_samples` against a test DB.
- **Golden** — a fixture set of labeled samples with a **stubbed** `reevaluate`,
  asserting exact **myopic** Brier / log-loss / skill / calibration / parse-
  failure outputs, and a multi-sample-per-market fixture asserting **trajectory**
  replay threads priors correctly (step N's prior == step N−1's output) and
  computes the right final-belief skill + convergence series. No API calls.
- **Search-off enforcement** — a candidate config with `web_search_max_uses > 0`
  is force-zeroed by the harness (assert the effective config passed to
  `reevaluate` has search disabled).
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

- `db/migrations/0005_reeval_samples.sql` — new `reeval_configs` + `reeval_samples`
  tables.
- `lib/reeval.py` — new; `ReevalConfig` (+ `config_hash`, `to_dict`/`from_dict`)
  and `reevaluate()` (absorbs `lib/claude.py` re-eval logic).
- `lib/claude.py` — reduced to the Anthropic client / parsing helpers reused by
  `lib/reeval.py`, or folded into it.
- `lib/db.py` — `get_or_create_reeval_config`, `insert_reeval_sample`,
  `labeled_reeval_samples`.
- `services/worker/main.py` — build a `ReevalConfig` from settings; get-or-create
  its config row; capture a sample per matched market (fail-open).
- `scripts/backtest_reeval.py` — new; the offline harness (myopic + trajectory,
  web search force-disabled, convergence output).
- `configs/backtest_reeval.json` — example candidate sweep.
- `requirements.txt` (optional extra) — matplotlib, only for PNG convergence
  graphs; the harness runs and emits series without it.
- Tests under the repo's existing test layout.
