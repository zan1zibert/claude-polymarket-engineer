-- 0005 — signals + paper_positions (the trading loop).
--
-- The scorer closed the *forecasting* loop: it grades our belief against the
-- outcome. Nothing closed the *trading* loop, because nothing ever compared a
-- belief to the live price and committed to a position. These two tables are
-- that record.
--
--   signals         — one row per fired signal: the decision and every number it
--                     was made from. Append-only; an audit trail of intent.
--   paper_positions — the simulated fill and its lifecycle, settled against
--                     markets.resolved_outcome once the market resolves.
--
-- The split matters: a signal can fire on a market we already hold, in which case
-- the signals row is written and no position is opened. Keeping intent (signals)
-- separate from exposure (paper_positions) means that case is visible instead of
-- silently dropped.
--
-- Derived metrics (expected_roi, kelly, sharpe) are stored even though nothing
-- gates on them. They are pure functions of edge and cost_basis, so recomputing
-- them later would be possible — but storing them means a P&L query can bucket by
-- them directly, and the position-sizing algorithm that comes next can be fitted
-- against realised outcomes without a backfill.

CREATE TABLE IF NOT EXISTS signals (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id      TEXT NOT NULL REFERENCES markets(id),
    market_title   TEXT NOT NULL,
    rule           TEXT NOT NULL,              -- entry rule that fired ('conviction_edge')
    source         TEXT NOT NULL,              -- 'belief_update' | 'sweep'
                                               -- (`source`, not `trigger` — PG keyword)
    article_url    TEXT,                       -- newest belief_updates article;
                                               -- NULL if the worker never moved this belief
    belief         DOUBLE PRECISION NOT NULL,  -- markets.current_score at decision time
    yes_price      DOUBLE PRECISION NOT NULL,  -- live Gamma YES price at decision time
    side           TEXT NOT NULL,              -- 'YES' | 'NO' — the side we would buy
    cost_basis     DOUBLE PRECISION NOT NULL,  -- c: price of our side per share
    edge           DOUBLE PRECISION NOT NULL,  -- q - c, positive by construction
    win_prob       DOUBLE PRECISION NOT NULL,  -- q: our P(our side wins)
    expected_roi   DOUBLE PRECISION NOT NULL,  -- edge / c
    kelly          DOUBLE PRECISION NOT NULL,  -- edge / (1 - c)
    sharpe         DOUBLE PRECISION NOT NULL,  -- edge / sqrt(q(1-q))
    end_date       TIMESTAMPTZ,
    horizon_days   DOUBLE PRECISION
);

-- "every signal for this market, newest first" — the one hot read.
CREATE INDEX IF NOT EXISTS signals_market_idx ON signals (market_id, ts DESC);

CREATE TABLE IF NOT EXISTS paper_positions (
    id            BIGSERIAL PRIMARY KEY,
    signal_id     BIGINT NOT NULL REFERENCES signals(id),
    market_id     TEXT NOT NULL REFERENCES markets(id),
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    side          TEXT NOT NULL,               -- 'YES' | 'NO'
    entry_price   DOUBLE PRECISION NOT NULL,   -- cost basis per share at entry
    stake         DOUBLE PRECISION NOT NULL,   -- euros committed
    shares        DOUBLE PRECISION NOT NULL,   -- stake / entry_price
    status        TEXT NOT NULL DEFAULT 'open', -- 'open' | 'settled' | 'closed_early'
    closed_at     TIMESTAMPTZ,
    exit_price    DOUBLE PRECISION,            -- 1.0/0.0 on settlement; live price on early exit
    exit_reason   TEXT,                        -- 'resolved'; future: 'profit_target'
    pnl           DOUBLE PRECISION             -- euros: shares * exit_price - stake
);

-- One open position per market, enforced by the DATABASE rather than by
-- application code. A partial unique index makes double entry impossible even if
-- the notification path and the sweep path race, or the service restarts
-- mid-cycle. Being partial on status='open' means closed history never blocks a
-- legitimate re-entry after settlement.
CREATE UNIQUE INDEX IF NOT EXISTS paper_positions_one_open_idx
    ON paper_positions (market_id) WHERE status = 'open';

-- The settlement work queue: open positions, joined to their market's outcome.
CREATE INDEX IF NOT EXISTS paper_positions_open_idx
    ON paper_positions (market_id) WHERE status = 'open';
