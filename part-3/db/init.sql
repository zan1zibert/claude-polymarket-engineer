-- Schema for the relational + vector store (one Postgres, pgvector extension).
--
-- Loaded automatically by the postgres container on first start (mounted into
-- /docker-entrypoint-initdb.d/). Holds two things the worker touches:
--   markets        — current market state + its embedding (vector search target)
--   belief_updates — append-only audit history of every re-evaluation
--
-- Market ingestion (fetch from Gamma, embed, upsert) is out of scope here; this
-- file only creates the structure. `db/seed_markets.py` inserts dev fixtures.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS markets (
    id            TEXT PRIMARY KEY,         -- Gamma market id
    question      TEXT NOT NULL,            -- market title/question
    description   TEXT NOT NULL DEFAULT '',
    end_date      TIMESTAMPTZ,
    current_score DOUBLE PRECISION,         -- our belief, 0..1 (NULL until first eval)
    embedding     VECTOR(1024) NOT NULL,    -- voyage-3.5 @ 1024 dims
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Approximate-nearest-neighbour index for cosine distance (the `<=>` operator).
CREATE INDEX IF NOT EXISTS markets_embedding_idx ON markets
    USING hnsw (embedding vector_cosine_ops);

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
