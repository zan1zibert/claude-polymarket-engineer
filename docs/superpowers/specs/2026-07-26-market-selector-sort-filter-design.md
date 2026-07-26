# Market-Detail Selector: Sort & Filter — Design

**Date:** 2026-07-26
**Status:** Approved (pending implementation plan)

## Problem

The market-detail dashboard's `$market_id` selector lists markets ordered only
by `end_date`, labelled by slug. In practice almost every market has **zero
belief updates** (the worker never re-evaluated it), so the dropdown is mostly
noise and there's no way to surface the markets actually worth inspecting — the
ones we've re-evaluated, or where our belief diverges most from the market.

## Goals

- Sort the market list by **number of belief updates** (↑/↓).
- Sort by **divergence** = `abs(our belief − market price)`, i.e. how far our
  current belief sits from the current market price (↑/↓).
- Sort by **days to resolution** = `end_date` (↑ soonest / ↓ furthest).
- Keep **recent** (by `end_date`) as the default-style ordering.
- Filter to **markets with ≥1 belief update**, to hide the zero-update noise.
- Show the sorted-by value in each dropdown label so it's readable in place.

## Non-goals

- No changes to any panel — this is the selector only.
- No signed divergence (magnitude only). No sort by liquidity/volume/other
  columns (YAGNI).
- "Divergence" is a live, per-market scalar (current belief vs latest price);
  it is deliberately distinct from the resolved-only "edge" time series shown in
  the *Edge over the market* panel. The two are different concepts and the
  naming keeps them separate.

## Terminology

**Divergence** — `abs(markets.current_score − latest market_prices.yes_price)`
for a market. Available for any market that has at least one recorded price and
a non-null belief. Chosen over "edge" to avoid colliding with the edge panel's
meaning (resolved belief-error advantage).

## Architecture

Presentation-only change to a single file:
`part-3/monitoring/grafana/dashboards/market-detail.json`. Two new Grafana
custom template variables feed into a rewritten `$market_id` query. No schema,
service, or datasource changes. Data comes from existing tables (`markets`,
`belief_updates`, `market_prices`).

## Template variables

Order in the top bar: `Status` → `Activity` → `Sort` → `Market`.

**`$status`** — unchanged (custom): `All` / `Open` / `Resolved`.

**`$activity`** — new custom variable, options (text : value):
- `All markets` : `all`
- `Has belief updates` : `active`
- **Default: `active`** (`Has belief updates`).

**`$sort`** — new custom variable, options (text : value):
- `Recent` : `recent`
- `Belief updates ↓` : `updates_desc`
- `Belief updates ↑` : `updates_asc`
- `Divergence ↓` : `div_desc`
- `Divergence ↑` : `div_asc`
- `Days to resolution ↓` : `res_desc` (furthest-out `end_date` first)
- `Days to resolution ↑` : `res_asc` (soonest `end_date` first)
- **Default: `updates_desc`** (`Belief updates ↓`).

Defaults are chosen so the dashboard opens onto the re-evaluated markets,
most-active first. Flipping `$activity` to `All markets` restores the full list.

## Rewritten `$market_id` query

```sql
SELECT m.id AS __value,
       COALESCE(m.slug, m.question)
         || '  · ' || COALESCE(u.n,0) || ' upd'
         || CASE WHEN px.yes_price IS NOT NULL AND m.current_score IS NOT NULL
                 THEN '  · Δ' || to_char(abs(m.current_score - px.yes_price),'FM0.00')
                 ELSE '' END AS __text
FROM markets m
LEFT JOIN (SELECT market_id, count(*) n FROM belief_updates GROUP BY market_id) u
       ON u.market_id = m.id
LEFT JOIN LATERAL (SELECT yes_price FROM market_prices p
                   WHERE p.market_id = m.id ORDER BY ts DESC LIMIT 1) px ON true
WHERE (('$status'='All')
       OR ('$status'='Open' AND NOT m.closed)
       OR ('$status'='Resolved' AND m.closed AND m.resolved_outcome IS NOT NULL))
  AND ('$activity'='all' OR COALESCE(u.n,0) > 0)
ORDER BY
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

Grafana substitutes `$sort`/`$status`/`$activity` as text into the SQL. Each
sort option owns one `CASE`-guarded `ORDER BY` term: only the matching option
yields non-null values, every other term is NULL for all rows and (with
`NULLS LAST`) contributes nothing to the ordering. The trailing
`(m.end_date IS NULL), m.end_date DESC` is the `Recent` ordering and the
tiebreak for the others. This keeps direction (`ASC`/`DESC`) correct per option
without storing SQL fragments in variable values.

### Label enrichment

Each entry always shows the update count (`· N upd`) and, when a price and
belief exist, the divergence (`· Δ0.23`), e.g.
`will-team-a-win-championship · 5 upd · Δ0.23`.

## Edge cases

- **No price / no belief:** `px.yes_price` or `current_score` NULL → divergence
  is NULL → those markets sort to the bottom under `NULLS LAST`, and the `Δ`
  suffix is omitted from the label.
- **Zero belief updates:** `u.n` is NULL via the LEFT JOIN → `COALESCE(...,0)`
  shows `0 upd`; excluded when `$activity='active'`.
- **`LIMIT 500`** retained — with `Has belief updates` the active set is far
  smaller, so the cap is effectively never hit for the useful views.
- **Performance:** the grouped count and lateral latest-price run over a few
  hundred markets and are backed by existing indexes
  (`belief_updates (market_id, ts DESC)`, `market_prices` PK `(market_id, ts)`).

## Testing / verification

On local Grafana with the `verify-*` fixtures and against the real ~223-market
data:
- `Belief updates ↓/↑` order by count correctly; labels show `· N upd`.
- `Divergence ↓/↑` order by `abs(belief − price)`; labels show `· Δx.xx`;
  null-metric markets fall to the bottom.
- `Days to resolution ↓/↑` order by `end_date` (furthest / soonest first);
  NULL `end_date` markets fall to the bottom.
- `Recent` matches the previous `end_date` ordering.
- `Has belief updates` hides all zero-update markets; `All markets` restores
  them; combines correctly with each `Status` value.
- Selecting a market still drives every panel (no regression).

## Files touched

- **Edit:** `part-3/monitoring/grafana/dashboards/market-detail.json`
  (add `$activity` and `$sort` template variables; rewrite the `$market_id`
  query; set the two new defaults).
