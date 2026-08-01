-- 0004 — relevance_checks (audit log for the Groq relevance filter).
--
-- The worker used to filter pgvector's top_k_markets candidates with a
-- cosine-distance threshold (max_cosine_distance). That threshold catches
-- topical/vocabulary proximity, not event identity, so genuinely unrelated
-- markets routinely passed the gate and reached Claude. This table replaces
-- the threshold with a per-candidate Groq relevance verdict, and records
-- EVERY verdict (accepted, rejected, or a Groq failure) so the filter's
-- precision can be reviewed from real data instead of guessed at.
--
-- market_id is a real FK: every row originates from a top_k_markets
-- candidate, so the referenced market always exists.

CREATE TABLE IF NOT EXISTS relevance_checks (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    article_url   TEXT NOT NULL,
    article_title TEXT NOT NULL,
    market_id     TEXT NOT NULL REFERENCES markets(id),
    relevant      BOOLEAN NOT NULL,
    reasoning     TEXT NOT NULL,
    model         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS relevance_checks_market_idx
    ON relevance_checks (market_id, ts DESC);
