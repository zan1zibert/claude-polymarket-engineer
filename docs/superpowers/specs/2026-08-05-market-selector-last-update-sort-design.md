# Market-Detail Selector: "Last update" Sort — Design

**Date:** 2026-08-05
**Status:** Approved (pending implementation plan)

## Problem

The `$market_id` selector on the market-detail dashboard (see
`2026-07-26-market-selector-sort-filter-design.md`) already supports sorting by
`Recent` (`end_date`), `Belief updates` (count of updates, ↑/↓), `Divergence`
(↑/↓), and `Days to resolution` (↑/↓). None of these sort by *when* a market
was last re-evaluated. There is no way to jump straight to the markets the
worker touched most recently — which is the natural thing to check when
watching the pipeline live.

The Polymarket-link/copyable-slug feature requested alongside this is already
implemented (the "↗ Open on Polymarket" panel on the same dashboard); no
changes are needed for that.

## Goals

- Add a sort option that orders markets by the timestamp of their most recent
  `belief_updates` row, both directions (most-recent-first and
  least-recent-first).
- Markets with zero belief updates sort to the bottom in both directions.
- Follow the exact `CASE`-guarded `ORDER BY` pattern already used by every
  other `$sort` option, so behavior and performance stay consistent.

## Non-goals

- No changes to the `$status`/`$activity` variables, panels, or the
  Polymarket link — out of scope.
- No change to the meaning of the existing `Recent` option (`end_date`).

## Architecture

Presentation-only change to a single file:
`part-3/monitoring/grafana/dashboards/market-detail.json`. Extends the
existing `u` subquery (which already computes `count(*) AS n` grouped by
`market_id`) to also compute `max(ts) AS last_update`. Adds two new `$sort`
options that consume it. No schema, service, or datasource changes.

## Template variable change

**`$sort`** — add two options (text : value), inserted after `Recent`:
- `Last update ↓` : `upd_ts_desc`
- `Last update ↑` : `upd_ts_asc`

Existing options and the default (`updates_desc`, i.e. `Belief updates ↓`)
are unchanged.

## Rewritten `$market_id` query

```sql
SELECT m.id AS __value,
       COALESCE(m.slug, m.question)
         || '  · ' || COALESCE(u.n,0) || ' upd'
         || CASE WHEN px.yes_price IS NOT NULL AND m.current_score IS NOT NULL
                 THEN '  · Δ' || to_char(abs(m.current_score - px.yes_price),'FM0.00')
                 ELSE '' END AS __text
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n, max(ts) last_update
           FROM belief_updates GROUP BY market_id) u
       ON u.market_id = m.id
LEFT JOIN LATERAL (SELECT yes_price FROM market_prices p
                   WHERE p.market_id = m.id ORDER BY ts DESC LIMIT 1) px ON true
WHERE (('$status'='All')
       OR ('$status'='Open' AND NOT m.closed)
       OR ('$status'='Resolved' AND m.closed AND m.resolved_outcome IS NOT NULL))
  AND ('$activity'='all' OR COALESCE(u.n,0) > 0)
ORDER BY
  CASE WHEN '$sort'='upd_ts_desc'  THEN u.last_update END DESC NULLS LAST,
  CASE WHEN '$sort'='upd_ts_asc'   THEN u.last_update END ASC  NULLS LAST,
  CASE WHEN '$sort'='updates_desc' THEN COALESCE(u.n,0) END DESC NULLS LAST,
  CASE WHEN '$sort'='updates_asc'  THEN COALESCE(u.n,0) END ASC  NULLS LAST,
  CASE WHEN '$sort'='div_desc'     THEN abs(m.current_score - px.yes_price) END DESC NULLS LAST,
  CASE WHEN '$sort'='div_asc'      THEN abs(m.current_score - px.yes_price) END ASC  NULLS LAST,
  CASE WHEN '$sort'='res_desc'     THEN m.end_date END DESC NULLS LAST,
  CASE WHEN '$sort'='res_asc'      THEN m.end_date END ASC  NULLS LAST,
  (m.end_date IS NULL), m.end_date DESC
LIMIT 500
```

### How the sort works

Same mechanism as the existing options: each `$sort` value owns one
`CASE`-guarded `ORDER BY` term; only the matching term is non-null across all
rows, every other term is NULL and (with `NULLS LAST`) contributes nothing.
`u.last_update` is NULL for markets with zero belief updates, so both
`upd_ts_desc` and `upd_ts_asc` push them to the bottom.

## Edge cases

- **Zero belief updates:** `u.last_update` is NULL via the `LEFT JOIN` →
  sorts last under both `upd_ts_desc` and `upd_ts_asc`.
- **Ties** (same `last_update`, unlikely but possible): fall through to the
  trailing `(m.end_date IS NULL), m.end_date DESC` tiebreak, same as every
  other sort option.
- **Performance:** `max(ts)` is computed in the same grouped subquery already
  used for the update count — no new join, no new table scan. Backed by the
  existing `belief_updates (market_id, ts DESC)` index.

## Testing / verification

On local Grafana with the `verify-*` fixtures and against the real ~223-market
data:
- `Last update ↓` shows the market with the most recent `belief_updates.ts`
  first; `Last update ↑` shows the oldest-updated market first (among markets
  with ≥1 update).
- Markets with zero belief updates appear last under both directions.
- Combines correctly with `$status` and `$activity` (e.g. `Has belief
  updates` + `Last update ↓` shows only updated markets, freshest first).
- Other `$sort` options (`Recent`, `Belief updates`, `Divergence`, `Days to
  resolution`) are unaffected.
- Selecting a market still drives every panel (no regression).

## Files touched

- **Edit:** `part-3/monitoring/grafana/dashboards/market-detail.json`
  (extend the `u` subquery with `max(ts) AS last_update`; add two `$sort`
  options; add two `ORDER BY` terms).
