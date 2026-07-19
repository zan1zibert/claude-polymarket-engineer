-- 0003 — forecast scores (the scoreboard that closes the loop).
--
-- Until now the pipeline was open-loop: the worker overwrites markets.current_score
-- (our belief) as news arrives, but nothing ever checked whether that belief was
-- right. Once a market resolves (markets.resolved_outcome becomes 0/1), the scorer
-- service grades our final belief against the truth AND against the market's own
-- price at ingest (markets.seed_price), and writes one row here per market.
--
-- One row per market (market_id PRIMARY KEY) makes scoring idempotent by
-- construction: the scorer's work queue is "resolved markets with no row here yet"
-- (see lib/db.resolved_unscored_markets), so a market is scored exactly once and a
-- re-run / overlapping cycle can't double-count.
--
-- We store BOTH our belief's scores and the baseline's scores so the headline
-- "are we beating the market?" skill score (1 - brier_belief/brier_baseline) is
-- computable in one query, and each market is inspectable on its own. n_updates
-- records how many times the worker actually moved this belief: a market with
-- n_updates = 0 was never touched (belief == seed_price) and acts as a control.

CREATE TABLE IF NOT EXISTS forecast_scores (
    market_id        TEXT PRIMARY KEY REFERENCES markets(id),
    scored_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome          DOUBLE PRECISION NOT NULL,   -- 1.0 YES won, 0.0 NO won
    final_belief     DOUBLE PRECISION NOT NULL,   -- our last current_score
    seed_price       DOUBLE PRECISION,            -- market baseline at ingest (may be NULL)
    n_updates        INT NOT NULL,                -- how many belief_updates the worker made
    brier_belief     DOUBLE PRECISION NOT NULL,
    logloss_belief   DOUBLE PRECISION NOT NULL,
    brier_baseline   DOUBLE PRECISION,            -- NULL when seed_price is unknown
    logloss_baseline DOUBLE PRECISION
);

-- Time-ordered reads for "scores over time" dashboards.
CREATE INDEX IF NOT EXISTS forecast_scores_scored_at_idx
    ON forecast_scores (scored_at DESC);
