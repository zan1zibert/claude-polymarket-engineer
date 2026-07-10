-- 0001 — baseline schema for the relational + vector store (one Postgres,
-- pgvector extension). Applied by db/migrate.py (the `migrate` service), not by
-- the postgres container's initdb hook. Holds two things the worker touches:
--   markets        — current market state + its embedding (vector search target)
--   belief_updates — append-only audit history of every re-evaluation
--
-- The `markets` table is kept in sync with Polymarket by the market-syncer
-- service (services/syncer/), which fetches, embeds, and marks resolved markets
-- closed (rows are retained for scoring, not deleted).
--
-- This is the baseline: it recreates the full current schema on a fresh DB, and
-- is a safe no-op (IF NOT EXISTS guards) against a DB already at this shape.
-- Later changes go in new numbered files — never edit an applied migration.
-- `db/seed_markets.py` inserts a couple of fixtures for a quick worker
-- smoke-test without running the syncer.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS markets (
    id            TEXT PRIMARY KEY,         -- Gamma market id
    question      TEXT NOT NULL,            -- market title/question
    description   TEXT NOT NULL DEFAULT '',
    end_date      TIMESTAMPTZ,              -- date_of_resolution (Gamma endDate)
    current_score DOUBLE PRECISION,         -- our belief, 0..1; seeded by the
                                            -- syncer with the Polymarket yes-price,
                                            -- then overwritten by the worker
    seed_price    DOUBLE PRECISION,         -- immutable Polymarket yes-price at ingest;
                                            -- the scoring baseline, never overwritten
    slug          TEXT,                     -- Polymarket slug (human-readable id)
    volume_24h    DOUBLE PRECISION,         -- rolling 24h volume (refreshed on sync)
    liquidity     DOUBLE PRECISION,         -- order-book liquidity (refreshed on sync)
    closed        BOOLEAN NOT NULL DEFAULT FALSE,  -- resolved on Polymarket; kept for
                                            -- scoring but excluded from retrieval
    resolved_at   TIMESTAMPTZ,              -- when the syncer marked it closed
    resolved_outcome DOUBLE PRECISION,      -- 1.0 if YES won, 0.0 if NO won; NULL until
                                            -- known or if the outcome isn't determinable
    embedding     VECTOR(1024) NOT NULL,    -- voyage-3.5 @ 1024 dims
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Forward-compat for databases created before these columns existed
-- (CREATE TABLE IF NOT EXISTS above won't alter an existing table).
ALTER TABLE markets ADD COLUMN IF NOT EXISTS slug        TEXT;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS volume_24h  DOUBLE PRECISION;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS liquidity   DOUBLE PRECISION;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS closed      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS seed_price       DOUBLE PRECISION;
ALTER TABLE markets ADD COLUMN IF NOT EXISTS resolved_outcome DOUBLE PRECISION;

-- The worker retrieves only open markets; partial index keeps that scan tight.
CREATE INDEX IF NOT EXISTS markets_open_idx ON markets (id) WHERE NOT closed;

-- Approximate-nearest-neighbour index for cosine distance (the `<=>` operator).
CREATE INDEX IF NOT EXISTS markets_embedding_idx ON markets
    USING hnsw (embedding vector_cosine_ops);

-- Resolved markets still awaiting an outcome — the scorer/backfill set.
CREATE INDEX IF NOT EXISTS markets_awaiting_outcome_idx ON markets (id)
    WHERE closed AND resolved_outcome IS NULL;

CREATE TABLE IF NOT EXISTS belief_updates (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id      TEXT NOT NULL REFERENCES markets(id),
    market_title   TEXT NOT NULL,
    previous_score DOUBLE PRECISION,        -- NULL on the very first evaluation
    new_score      DOUBLE PRECISION NOT NULL,
    article_url    TEXT NOT NULL,
    reasoning      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS belief_updates_market_idx
    ON belief_updates (market_id, ts DESC);
