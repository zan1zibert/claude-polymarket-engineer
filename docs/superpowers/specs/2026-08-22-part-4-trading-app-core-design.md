# Part 4 — Core Trading Layer (Design)

## Context

`part-3` forecasts market probabilities from news and, today, only opens **paper**
positions via its `signal` service — no real order is ever placed. The user wants a
genuinely new capability: a local web app to **browse markets, place real manual
buy/sell orders on Polymarket, and monitor balance/positions** — the foundation a
later automated stop-loss/stop-gain and conditional-trading-plan engine will build on.

This document specs only the **core trading layer** (manual browse + trade). The
automation/rules engine (stop-loss, stop-gain, conditional plans like "buy YES if
price ≥ 80% and resolves within 2 weeks") is an explicitly separate, sequential
follow-on spec once this layer's execution primitives exist.

## Decisions made during brainstorming

- **Real money, small scale.** This places actual orders on Polymarket's CLOB using
  a funded wallet — not paper/simulated.
- **Interface:** a web app, run locally only (bound to `localhost`, never exposed
  publicly).
- **Location:** new folder in this repo, `part-4/`, not a separate repository. No
  changes to `part-3`.
- **Key management (v1):** private key + CLOB API credentials via a local `.env` in
  `part-4/`, following the same pattern as `ANTHROPIC_API_KEY` etc. in `part-3`.
  Acceptable because the app never listens beyond localhost.
- **Market data for browsing:** reuse `part-3`'s existing synced `markets` table
  (Postgres) read-only, rather than re-syncing from Polymarket independently. Gets
  belief/edge context for free; inherits the syncer's existing filters
  (volume/liquidity/resolution window).
- **Frontend stack:** React + TypeScript SPA (not server-rendered templates).
- **Order types (v1):** market orders only. No resting limit orders, no open-order
  management — simplest to build and matches how a future stop-loss/stop-gain would
  fire anyway (immediate execution on trigger).
- **Position/balance source of truth:** always query Polymarket live (Data API /
  CLOB), never a locally-maintained ledger. No drift risk, no reconciliation logic.
- **Deployment:** standalone host process (`uvicorn` + `npm run dev`/build), not
  containerized, not added to `part-3`'s `docker-compose.yml`. Fastest to iterate;
  avoids Docker/network ceremony for a single-user local tool.

## Architecture

```
Browser (React SPA, localhost)
        │  REST/JSON
        ▼
part-4/backend (FastAPI, run via uvicorn on your machine)
    ├─▶ part-3 Postgres (read-only)      — market list, prices, belief/edge context
    └─▶ Polymarket CLOB + Data API       — live prices, balances, positions, order placement
              (via py-clob-client, using your wallet key from part-4/.env)
```

Two new pieces, no changes to `part-3`: a FastAPI backend in `part-4/backend/` and a
React/TypeScript frontend in `part-4/frontend/`. The backend is the only thing
holding the private key and CLOB API credentials — the browser never sees them. It
has a read-only Postgres connection to `part-3`'s DB (separate `DATABASE_URL`,
ideally a read-only Postgres role) purely for market browsing context; it never
writes there. All money-moving calls (balance, positions, place order) go straight
to Polymarket, live.

## Components

### Backend (`part-4/backend/`, FastAPI)

- `polymarket_client.py` — thin wrapper around `py-clob-client`: `get_balance()`,
  `get_positions()`, `get_price(token_id)`, `place_market_order(token_id, side,
  size)`. Isolates the one place that touches funds.
- `markets_repo.py` — read-only queries against `part-3`'s `markets` table
  (list/filter by category, price range, volume, resolution window), plus the
  latest belief/edge if available.
- `routes/markets.py` — `GET /markets`, `GET /markets/{id}` (merges the DB row with
  a live price refresh).
- `routes/trading.py` — `GET /account/balance`, `GET /account/positions`, `POST
  /orders` (market buy/sell — validates side/size, calls the client wrapper, returns
  the fill or a clear error).
- Config via `.env` (private key, CLOB API creds, `part-3`'s read-only
  `DATABASE_URL`), following the same `lib/config.py`-style pattern `part-3` already
  uses.

### Frontend (`part-4/frontend/`, React + TypeScript, e.g. Vite)

- **Market list/browse view** — filter/sort, backed by `GET /markets`.
- **Market detail view** — live price, belief/edge if available, Buy YES / Buy NO /
  Sell actions with an explicit confirm step (real money — no one-click accidental
  orders).
- **Positions/account view** — balance + open positions, fetched live on each visit;
  no local caching of money-relevant state.

No auth/login: single-user app bound to `localhost` only.

## Data flow: placing a manual order

1. Frontend requests `GET /markets/{id}` → backend merges the DB row with a fresh
   price pulled from Polymarket (never trusts a stale DB price for a trade
   decision).
2. User picks Buy YES / Buy NO / Sell, enters a USDC size, clicks — frontend shows a
   confirm dialog with the live price and estimated cost/proceeds before sending
   anything.
3. On confirm, frontend calls `POST /orders {market_id, token_id, side, size}`.
4. Backend re-fetches the live price server-side (never trusts a client-supplied
   price), builds and signs a market order via `py-clob-client`, submits it.
5. Backend returns the fill result (executed price, size, order id) or a structured
   error; frontend shows it plainly — no silent retries on a money-moving call.

## Error handling

- **Order failures** (insufficient balance, price moved past a slippage bound,
  network/API error from Polymarket): surfaced verbatim to the UI, never retried
  automatically — the user decides whether to resubmit.
- **Slippage guard**: since it's a market order, the backend enforces a max
  acceptable slippage vs. the price it just quoted (e.g. reject if the fill would be
  worse than X%) rather than trusting Polymarket's FOK matching blindly.
- **DB unavailable**: market browsing degrades gracefully (error banner on the
  list), but this never blocks the account/positions/order endpoints — those only
  depend on Polymarket, not on `part-3`'s Postgres.
- **Wallet/key misconfigured**: fail fast at backend startup with a clear message,
  not on first order attempt.

## Testing

- Unit tests for `polymarket_client.py` against recorded/mocked CLOB responses
  (order payload construction, slippage check, error mapping) — no real orders in
  CI.
- Unit tests for `markets_repo.py` against a test Postgres (same pattern `part-3`
  already uses: skipped unless `TEST_DATABASE_URL` is set).
- Manual smoke test plan for the real-money path: place one small real order (e.g.
  $1) against a liquid market and verify balance/positions reflect it — documented
  as a manual step, not automated, since it moves real funds.

## Out of scope (this spec)

- Stop-loss / stop-gain and conditional trading plans (next spec, builds on this
  layer's `place_market_order` primitive).
- Limit orders / open-order management.
- Any containerization or integration into `part-3`'s `docker-compose.yml`.
- Multi-user auth (single local user only).
