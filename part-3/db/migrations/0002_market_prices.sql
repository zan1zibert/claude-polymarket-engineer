-- 0002 — market price time series.
--
-- Until now we stored only two price points per market: seed_price (the
-- Polymarket YES price at ingest) and, once resolved, the 0/1 outcome. That is
-- enough to ask "were we better than the market at ingest?" and "were we right
-- in the end?" — but NOT "what was the market price at the moment our belief
-- diverged?", which is the question the edge / front-running thesis and any
-- point-in-time backtest actually turn on.
--
-- The syncer already fetches the live YES price for every open market each cycle
-- (it queries Gamma for resolution status anyway), so it appends an observation
-- here. Only open markets are recorded: a resolved market's price is pinned at
-- 0/1 and carries no information. Rows accumulate append-only; the syncer skips
-- writing when the price hasn't moved (see lib/db.record_prices), so a row means
-- "the price changed", which is what a backtest cares about.
--
-- The (market_id, ts) primary key also serves the one hot query a backtest needs
-- — "price as of t":  WHERE market_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1.

CREATE TABLE IF NOT EXISTS market_prices (
    market_id  TEXT NOT NULL REFERENCES markets(id),
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    yes_price  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (market_id, ts)
);
