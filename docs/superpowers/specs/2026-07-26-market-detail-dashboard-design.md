# Per-Market Detail Dashboard — Design

**Date:** 2026-07-26
**Status:** Approved (pending implementation plan)

## Problem

We want to inspect a single Polymarket market's trajectory over time: how the
**market's own price** and **our internal belief** evolve as the market
approaches resolution, and — for resolved markets — which of the two was closer
to the truth, and when.

A near-complete version of this already exists as the "Market detail —
convergence race" row (panels id 7 & 8) inside `accuracy.json`, but it has three
gaps:

1. **Resolved-only.** Its `$market_id` variable filters
   `WHERE closed AND resolved_outcome IS NOT NULL`, so open/live markets can't be
   inspected — and when no market has fully resolved, the dropdown is empty and
   **every panel shows "No data"** (the reported bug).
2. **No "who's winning" signal** for resolved markets.
3. **The market picker is keyed by id/description**, which is hard to scan.

It also muddles concerns: `accuracy.json` is meant to be the aggregate
scoreboard, not a single-market drill-down.

## Goals

- A dedicated per-market inspector covering **both open and resolved** markets.
- Market selector keyed by **slug** (concise + descriptive).
- Trajectory view: market price vs. our belief vs. outcome, with the
  **time-till-resolution** readable off the x-axis via a resolution marker.
- For resolved markets, a **threshold-colored "edge" panel** showing, segment by
  segment over time, whether our belief or the market price was closer to the
  eventual outcome.
- Fix the "No data" bug.

## Non-goals

- A true countdown (days-to-resolution) x-axis via an XY chart. We deliberately
  keep the native timeseries panel (wall-clock x) plus a resolution marker; the
  gap between the last data point and the marker conveys time remaining.
- Static green/red recoloring of the whole belief/price lines. Leadership flips
  over time, so "who's winning" is a time-varying property expressed in the edge
  panel, not a whole-line paint job.
- Any change to the data model or the syncer/worker/scorer services. This is a
  presentation-layer change; all required data already exists in `markets`,
  `belief_updates`, `market_prices`, and `forecast_scores`.

## Architecture

New Grafana dashboard, provisioned by the existing folder provider (no config
change needed):

- **File:** `part-3/monitoring/grafana/dashboards/market-detail.json`
- **uid:** `pm-market-detail`
- **title:** "Polymarket — Market Detail"
- **datasource:** existing provisioned `Postgres` (uid `postgres`)

The "Market detail — convergence race" row (panels id 7 & 8) is **removed from
`accuracy.json`**, leaving that dashboard purely aggregate. The trajectory panel
is rebuilt in the new dashboard with open-market support.

Default dashboard time range: `now-90d` → `now+30d` (the future portion makes the
resolution marker and the time-till-resolution gap visible for open markets;
adjustable via the time picker for markets resolving further out).

## Template variables

**`$market_id`** — query variable, value = id, displayed text = slug:

```sql
SELECT id AS __value, COALESCE(slug, question) AS __text
FROM markets
WHERE ('$status' = 'All')
   OR ('$status' = 'Open' AND NOT closed)
   OR ('$status' = 'Resolved' AND closed AND resolved_outcome IS NOT NULL)
ORDER BY (end_date IS NULL), end_date DESC
LIMIT 500
```

**`$status`** — custom variable, options `All` (default) / `Open` / `Resolved`,
to narrow the market list.

## Panels

### Row: Overview stats (top)

Small `stat` panels for the selected market:

- **Current belief** — `SELECT current_score FROM markets WHERE id = '$market_id'`
- **Current market price** — latest `market_prices.yes_price`:
  `SELECT yes_price FROM market_prices WHERE market_id = '$market_id' ORDER BY ts DESC LIMIT 1`
- **Days to resolution** —
  `SELECT EXTRACT(epoch FROM (end_date - now()))/86400 FROM markets WHERE id = '$market_id'`
  (negative once past `end_date`)
- **Updates** — `SELECT count(*) FROM belief_updates WHERE market_id = '$market_id'`
- **Brier skill (resolved)** — from `forecast_scores`:
  `SELECT 1 - brier_belief/NULLIF(brier_baseline,0) FROM forecast_scores WHERE market_id = '$market_id'`
  (empty until the market is scored)

### Panel: "Belief vs market price vs outcome" (timeseries)

Unit `percentunit`, min 0, max 1. Series:

- **`market price`** (orange, linear) —
  ```sql
  SELECT ts AS "time", yes_price AS "market price"
  FROM market_prices
  WHERE market_id = '$market_id' AND $__timeFilter(ts)
  ORDER BY ts
  ```
- **`our belief`** (blue, **stepAfter** — belief only moves on re-evaluation) —
  seeded at `seed_price`, then each `belief_updates.new_score`:
  ```sql
  SELECT time, "our belief" FROM (
    SELECT (SELECT min(ts) FROM market_prices WHERE market_id = '$market_id') AS time,
           (SELECT seed_price FROM markets WHERE id = '$market_id') AS "our belief"
    UNION ALL
    SELECT ts AS time, new_score AS "our belief"
    FROM belief_updates WHERE market_id = '$market_id'
  ) belief
  WHERE $__timeFilter(time)
  ORDER BY time
  ```
  stepAfter interpolation extends the last belief to the right edge (correct for
  open markets — belief holds until the next re-evaluation).
- **`outcome`** (gray, dashed, flat) — NULL/empty for open markets, so it simply
  draws nothing until resolution:
  ```sql
  SELECT $__timeFrom() AS "time", resolved_outcome AS "outcome"
  FROM markets WHERE id = '$market_id'
  UNION ALL
  SELECT $__timeTo(), resolved_outcome FROM markets WHERE id = '$market_id'
  ORDER BY time
  ```

**Resolution marker:** dashboard annotation (vertical dashed line) at `end_date`:

```sql
SELECT end_date AS time, 'resolution' AS text
FROM markets WHERE id = '$market_id' AND end_date IS NOT NULL
```

### Panel: "Edge over the market" (timeseries, resolved only)

Single series = `|market − outcome| − |belief − outcome|`, sampled at each price
observation using the belief in effect at that moment (lateral join to the latest
`belief_updates` ≤ ts, falling back to `seed_price`):

```sql
SELECT mp.ts AS "time",
       abs(m.resolved_outcome - mp.yes_price)
     - abs(m.resolved_outcome - COALESCE(b.belief, m.seed_price)) AS "edge"
FROM market_prices mp
JOIN markets m ON m.id = mp.market_id
LEFT JOIN LATERAL (
  SELECT new_score AS belief FROM belief_updates bu
  WHERE bu.market_id = mp.market_id AND bu.ts <= mp.ts
  ORDER BY bu.ts DESC LIMIT 1
) b ON true
WHERE mp.market_id = '$market_id'
  AND m.resolved_outcome IS NOT NULL
  AND $__timeFilter(mp.ts)
ORDER BY mp.ts
```

Interpretation: **positive = our belief is closer to truth than the market;
negative = the market is closer.** Threshold coloring: absolute thresholds with
green at ≥ 0 and red below 0, "from thresholds (by value)" color scheme, gradient
fill. Empty for open markets (no known outcome).

### Panel: Reasoning log (table)

Every belief move for the selected market, most recent first — the "why" behind
the trajectory:

```sql
SELECT ts AS "time", previous_score AS "from", new_score AS "to",
       article_url AS "article", reasoning
FROM belief_updates
WHERE market_id = '$market_id'
ORDER BY ts DESC
```

`article` rendered as a link; `reasoning` as wrapped text.

## The "No data" fix

Leading hypothesis: the current `$market_id` variable lists only fully-resolved
markets; with none present the dropdown is empty and all panels read "No data".
The new variable (lists open markets too) resolves this.

During implementation, before finalizing, bring the stack up and **confirm** this
is the actual cause rather than assuming. Secondary suspects to rule out:

1. Default time range (`now-30d`) excluding the market's price/belief history.
2. The Grafana Postgres datasource `POSTGRES_PASSWORD` env not being wired in the
   compose file (would surface as an auth error, not "No data", but verify).

## Testing / verification

- Bring up the dev stack (`docker-compose.yml`), seed/sync so at least one open
  and (ideally) one resolved market exist.
- Load the dashboard; confirm:
  - Slug selector lists markets by slug; `$status` filter works.
  - Open market: belief + price lines render, belief steps and holds to the right
    edge, resolution marker visible, outcome/edge empty, no "No data".
  - Resolved market: outcome line present, edge panel green where we lead / red
    where the market leads, Brier-skill stat populated.
  - Reasoning log lists belief moves with working article links.
- Confirm `accuracy.json` still loads with the convergence row removed.

## Files touched

- **Add:** `part-3/monitoring/grafana/dashboards/market-detail.json`
- **Edit:** `part-3/monitoring/grafana/dashboards/accuracy.json` (remove panels
  id 7 & 8 and their row)
