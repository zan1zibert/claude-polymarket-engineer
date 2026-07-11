"""Relational + vector store access (Postgres + pgvector).

Operations the worker needs:
  - top_k_markets   : nearest markets to an article embedding (read-only)
  - apply_belief_update : atomically swap a market's score and log the transition

Operations the market-syncer needs (services/syncer/):
  - existing_market_ids     : which of these ids do we already store?
  - insert_markets          : add new markets (score seeded from Polymarket price)
  - refresh_market_metadata : refresh volatile fields, leaving belief + embedding
  - record_prices           : append current YES-price observations (the price series)
  - open_market_ids         : every market still open — the resolution check set
  - mark_resolved           : flag resolved markets closed (rows kept for scoring)

Operations the scorer needs (services/scorer/):
  - resolved_unscored_markets : resolved markets with no forecast_scores row yet
  - insert_score              : write one market's grades (idempotent per market)
  - score_aggregates          : corpus-wide means for the Prometheus gauges

Concurrency note: the worker is scalable (×N), so two workers can re-score the
SAME market at once. The score swap therefore runs in a short transaction that
takes a row lock (`SELECT ... FOR UPDATE`), serialising writes per market and
re-reading the true prior under the lock so the audit chain stays truthful. The
expensive Claude call happens OUTSIDE this lock (see services/worker/main.py),
so the lock is held only for the two quick writes.
"""
from datetime import datetime, timezone
from typing import Optional

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from lib.schemas import BeliefUpdate, Market


class Db:
    def __init__(self, database_url: str):
        # autocommit so each read is its own txn; apply_belief_update opens an
        # explicit transaction for the locked read-modify-write.
        self._conn = psycopg.connect(database_url, autocommit=True)
        register_vector(self._conn)

    def ping(self) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() == (1,)

    def top_k_markets(
        self, embedding: list[float], k: int, max_distance: float
    ) -> list[Market]:
        """Markets nearest the embedding, dropping anything beyond the relevance gate.

        `<=>` is pgvector's cosine distance (0 = identical, 2 = opposite). We
        order by it, keep the k closest, then filter by `max_distance` so an
        article that matches nothing relevant returns an empty list.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, question, description, current_score
                FROM (
                    SELECT id, question, description, current_score,
                           embedding <=> %s AS distance
                    FROM markets
                    WHERE NOT closed
                    ORDER BY distance
                    LIMIT %s
                ) ranked
                WHERE distance <= %s
                """,
                (Vector(embedding), k, max_distance),
            )
            return [
                Market(id=r[0], question=r[1], description=r[2], current_score=r[3])
                for r in cur.fetchall()
            ]

    def apply_belief_update(
        self, market_id: str, new_score: float, article_url: str, reasoning: str
    ) -> BeliefUpdate:
        """Swap current_score and append a belief_updates row, atomically.

        Re-reads the prior under a row lock so concurrent workers can't clobber
        each other's audit chain. Returns the constructed BeliefUpdate for the
        caller to fan out to the queue + audit log.
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT question, current_score FROM markets WHERE id = %s FOR UPDATE",
                    (market_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"market {market_id} not found")
                title, previous_score = row

                cur.execute(
                    "UPDATE markets SET current_score = %s, updated_at = now() WHERE id = %s",
                    (new_score, market_id),
                )
                cur.execute(
                    """
                    INSERT INTO belief_updates
                        (ts, market_id, market_title, previous_score, new_score,
                         article_url, reasoning)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (ts, market_id, title, previous_score, new_score,
                     article_url, reasoning),
                )

        return BeliefUpdate(
            timestamp=ts,
            market_id=market_id,
            market_title=title,
            previous_score=previous_score,
            new_score=new_score,
            article_url=article_url,
            reasoning=reasoning,
        )

    # ----------------------------------------------------------------- syncer

    def existing_market_ids(self, ids: list[str]) -> set[str]:
        """Subset of `ids` already present, so the syncer only embeds new ones."""
        if not ids:
            return set()
        with self._conn.cursor() as cur:
            cur.execute("SELECT id FROM markets WHERE id = ANY(%s)", (ids,))
            return {r[0] for r in cur.fetchall()}

    def insert_markets(self, rows: list[dict]) -> int:
        """Insert new markets, seeding both current_score and the immutable seed_price
        with the Polymarket yes-price at ingest.

        current_score is the belief the worker later overwrites; seed_price is the
        scoring baseline and is never touched again. Each row carries an
        already-computed `embedding`. ON CONFLICT DO NOTHING makes this safe if a
        market was inserted concurrently; the caller is expected to have filtered to
        genuinely-new ids already.
        """
        if not rows:
            return 0
        inserted = 0
        with self._conn.cursor() as cur:
            for m in rows:
                cur.execute(
                    """
                    INSERT INTO markets
                        (id, question, description, end_date, current_score,
                         seed_price, slug, volume_24h, liquidity, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        m["id"], m["question"], m["description"],
                        m["end_date"] or None, m["yes_price"], m["yes_price"],
                        m["slug"], m["volume_24h"], m["liquidity"], Vector(m["embedding"]),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def record_prices(
        self, rows: list[tuple[str, float]], *, min_change: float = 0.0
    ) -> int:
        """Append current YES-price observations for open markets to market_prices.

        `rows` is (market_id, yes_price) pairs. Dedupe-on-change: an observation is
        written only if it differs from that market's most recent stored price by at
        least `min_change` (a market with no prior row is always written). This keeps
        the series meaningful — a row means "the price moved" — and stops flat,
        illiquid markets from filling the table with identical points. All rows in a
        cycle share one transaction timestamp, so they read as a coherent snapshot.
        Returns the number of observations written.
        """
        if not rows:
            return 0

        ids = [r[0] for r in rows]
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (market_id) market_id, yes_price
                FROM market_prices
                WHERE market_id = ANY(%s)
                ORDER BY market_id, ts DESC
                """,
                (ids,),
            )
            last = {r[0]: r[1] for r in cur.fetchall()}

        to_write = [
            (mid, price)
            for mid, price in rows
            if mid not in last or abs(price - last[mid]) >= min_change
        ]
        if not to_write:
            return 0

        # One transaction → now() is constant across the batch, so every row in this
        # cycle carries the same ts (a coherent price snapshot).
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO market_prices (market_id, yes_price) VALUES (%s, %s)",
                to_write,
            )
        return len(to_write)

    def open_market_ids(self) -> list[str]:
        """Ids of every market still open — the set we re-check against Gamma.

        We check all of them, not just those past `end_date`: Polymarket markets
        can resolve early ("will X by date D" settles the moment X happens) and
        some carry no end_date, so end_date is only an upper bound on resolution
        and can't decide what to check. Affordable because this set is our own
        holdings (a few hundred), not Polymarket's whole market list.
        """
        with self._conn.cursor() as cur:
            cur.execute("SELECT id FROM markets WHERE NOT closed")
            return [r[0] for r in cur.fetchall()]

    def mark_resolved(self, outcomes: dict[str, Optional[float]]) -> int:
        """Flag resolved markets closed and store their outcome, preserving the row.

        `outcomes` maps market_id -> resolved_outcome (1.0 if YES won, 0.0 if NO won,
        or None when the outcome isn't determinable — e.g. Gamma no longer returns the
        market). Closed markets are excluded from `top_k_markets` so the worker stops
        scoring them, but the row (and its belief_updates) is retained so a future
        scoring pass can grade our predictions against the outcome. Idempotent — only
        flips rows still open. Returns the number newly marked.
        """
        if not outcomes:
            return 0
        resolved = 0
        with self._conn.cursor() as cur:
            for market_id, outcome in outcomes.items():
                cur.execute(
                    """
                    UPDATE markets
                       SET closed = TRUE, resolved_at = now(), updated_at = now(),
                           resolved_outcome = %s
                     WHERE id = %s AND NOT closed
                    """,
                    (outcome, market_id),
                )
                resolved += cur.rowcount
        return resolved

    # ----------------------------------------------------------------- scorer

    def resolved_unscored_markets(self) -> list[dict]:
        """Markets ready to grade: resolved, with an outcome, not yet scored.

        The scorer's work queue. A market qualifies when it has a definitive
        outcome (`resolved_outcome IS NOT NULL`), a belief to grade
        (`current_score IS NOT NULL`), and no `forecast_scores` row yet — the
        LEFT JOIN ... IS NULL anti-join makes scoring exactly-once, so a re-run
        or an overlapping cycle never re-scores a market.

        Each dict carries what the pure scorer needs: the outcome (y), our
        `final_belief` (current_score) and the `seed_price` baseline (may be
        NULL), plus `n_updates` — how many times the worker moved this belief
        (0 => never touched; belief == seed_price, a control sample).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id,
                       m.resolved_outcome,
                       m.current_score,
                       m.seed_price,
                       (SELECT count(*) FROM belief_updates b WHERE b.market_id = m.id)
                FROM markets m
                LEFT JOIN forecast_scores s ON s.market_id = m.id
                WHERE m.closed
                  AND m.resolved_outcome IS NOT NULL
                  AND m.current_score IS NOT NULL
                  AND s.market_id IS NULL
                """
            )
            return [
                {
                    "market_id": r[0],
                    "outcome": r[1],
                    "final_belief": r[2],
                    "seed_price": r[3],
                    "n_updates": r[4],
                }
                for r in cur.fetchall()
            ]

    def insert_score(
        self,
        market_id: str,
        *,
        outcome: float,
        final_belief: float,
        seed_price: Optional[float],
        n_updates: int,
        brier_belief: float,
        logloss_belief: float,
        brier_baseline: Optional[float],
        logloss_baseline: Optional[float],
    ) -> bool:
        """Write one market's grades. Returns True if a row was inserted.

        ON CONFLICT DO NOTHING keeps it idempotent even though
        resolved_unscored_markets already filters scored markets out — a belt
        for the (singleton, so rare) case of two cycles racing on the same row.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO forecast_scores
                    (market_id, outcome, final_belief, seed_price, n_updates,
                     brier_belief, logloss_belief, brier_baseline, logloss_baseline)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (market_id) DO NOTHING
                """,
                (
                    market_id, outcome, final_belief, seed_price, n_updates,
                    brier_belief, logloss_belief, brier_baseline, logloss_baseline,
                ),
            )
            return cur.rowcount == 1

    def score_aggregates(self) -> dict:
        """Corpus-wide mean scores over every graded market (for the gauges).

        Returns means (None until at least one row exists) of belief and
        baseline Brier / log-loss, plus the row count. The baseline means cover
        only rows where a baseline was computable (seed_price known), so the
        skill score compares like with like. These are recomputed each cycle
        rather than incremented because a mean-over-the-corpus is a snapshot, not
        an event rate.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT avg(brier_belief), avg(logloss_belief),
                       avg(brier_baseline), avg(logloss_baseline),
                       count(*)
                FROM forecast_scores
                """
            )
            b_belief, ll_belief, b_base, ll_base, n = cur.fetchone()
            return {
                "brier_belief": b_belief,
                "logloss_belief": ll_belief,
                "brier_baseline": b_base,
                "logloss_baseline": ll_base,
                "count": n,
            }
