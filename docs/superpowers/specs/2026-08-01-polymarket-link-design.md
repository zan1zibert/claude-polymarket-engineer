# Market-Detail: One-Click Polymarket Link — Design

**Date:** 2026-08-01
**Status:** Approved (pending implementation plan)

## Problem

Browsing markets in the market-detail dashboard is easy, but jumping from a
selected market to its page on Polymarket is painful — there's no link, and
hunting for it by hand (or by the question text) is slow.

## Goal

From the market-detail dashboard, open the currently-selected market on
Polymarket in one click. The slug should also be visible so it can be
read/selected as a fallback.

## Non-goals

- No separate "copy to clipboard" button (Grafana has no native one; the visible
  slug is selectable, and the one-click link makes copying unnecessary).
- No capture of the Polymarket *event* slug (see URL note); we link via the
  market slug we already store.
- No changes to other panels, services, or the schema.

## Verified URL format

Confirmed against live Polymarket pages (two real market slugs, one plain and
one with a numeric suffix):

- `https://polymarket.com/market/<slug>` — **valid**, lands on the exact market.
- `https://polymarket.com/event/<slug>` — **404** (our stored `slug` is the
  Gamma *market* slug, not an event slug).

So the link base is **`https://polymarket.com/market/`** + `markets.slug`.
(`markets.slug` is populated by the syncer from the Gamma market object —
`lib/polymarket.py:normalize` → `market.get("slug")`.)

## Design

A new **stat panel** titled **"↗ Open on Polymarket"** on the market-detail
dashboard (`part-3/monitoring/grafana/dashboards/market-detail.json`).

- **Query:** `SELECT slug FROM markets WHERE id = '$market_id'` (datasource uid
  `postgres`, format `table`). The tile displays the slug text.
- **Data link:** URL `https://polymarket.com/market/${__data.fields.slug}`,
  **open in a new tab** (`targetBlank: true`), title e.g. "Open on Polymarket".
  Clicking the tile navigates to the market. The visible slug doubles as a
  read/select fallback — covering both "link" and "copy" from the original ask.
- **No new template variable** — the panel queries the slug directly from
  `$market_id`.

### Placement

The link sits in **its own thin, full-width row**, visually separate from the
5-tile overview stat row (which is left unchanged). It goes directly below the
overview stat tiles and above the "Belief vs market price vs outcome" panel; the
trajectory, edge, and reasoning-log panels shift down by the new row's height.
Exact `gridPos` values are an implementation detail for the plan.

## Edge cases

- **NULL slug** (rare — the syncer always sets it): the tile renders empty and
  the data link is malformed; this degrades to a non-working click, not an
  error. Acceptable for v1.

## Testing / verification

On local Grafana:
- The "↗ Open on Polymarket" tile renders on its own row and shows the selected
  market's slug.
- Its data link constructs `https://polymarket.com/market/<slug>` and opens in a
  new tab. (Local fixtures use synthetic slugs, so those specific links won't
  resolve on Polymarket — the base URL is already verified against real slugs;
  what we verify locally is correct URL *construction* and new-tab behavior.)
- One detail to confirm during implementation: whether Grafana renders the bare
  slug via `${__data.fields.slug}` in the data-link URL, or whether
  `${__value.text}` is needed instead — test both and use whichever produces
  `.../market/<slug>` correctly.
- Selecting different markets updates the link/slug; no other panel regresses.

## Files touched

- **Edit:** `part-3/monitoring/grafana/dashboards/market-detail.json` (add the
  "↗ Open on Polymarket" stat panel in its own row; shift the panels below it
  down).
