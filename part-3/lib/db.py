"""Relational + vector store access (Postgres + pgvector).

Operations the worker needs:
  - top_k_markets   : nearest markets to an article embedding (read-only)
  - apply_belief_update : atomically swap a market's score and log the transition

Operations the market-syncer needs (services/syncer/):
  - existing_market_ids     : which of these ids do we already store?
  - insert_markets          : add new markets (score seeded from Polymarket price)
  - refresh_market_metadata : refresh volatile fields, leaving belief + embedding
  - open_market_ids         : every market still open — the resolution check set
  - mark_resolved           : flag resolved markets closed (rows kept for scoring)

Concurrency note: the worker is scalable (×N), so two workers can re-score the
SAME market at once. The score swap therefore runs in a short transaction that
takes a row lock (`SELECT ... FOR UPDATE`), serialising writes per market and
re-reading the true prior under the lock so the audit chain stays truthful. The
expensive Claude call happens OUTSIDE this lock (see services/worker/main.py),
so the lock is held only for the two quick writes.
"""
from datetime import datetime, timezone

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
        """Insert new markets, seeding current_score with the Polymarket price.

        Each row carries an already-computed `embedding`. ON CONFLICT DO NOTHING
        makes this safe if a market was inserted concurrently; the caller is
        expected to have filtered to genuinely-new ids already.
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
                         slug, volume_24h, liquidity, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        m["id"], m["question"], m["description"],
                        m["end_date"] or None, m["yes_price"], m["slug"],
                        m["volume_24h"], m["liquidity"], Vector(m["embedding"]),
                    ),
                )
                inserted += cur.rowcount
        return inserted

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

    def mark_resolved(self, resolutions: dict[str, dict]) -> int:
        """Flag resolved markets closed and store their outcome, preserving the row.

        `resolutions` maps market_id -> {outcomes, outcome_prices}; either value may
        be None when Gamma no longer returns the market. Closed markets are excluded
        from `top_k_markets` so the worker stops scoring them, but the row (and its
        belief_updates) is retained so a future scoring pass can grade our predictions
        against the outcome. Idempotent — only flips rows still open. Returns the
        number newly marked.
        """
        if not resolutions:
            return 0
        resolved = 0
        with self._conn.cursor() as cur:
            for market_id, data in resolutions.items():
                cur.execute(
                    """
                    UPDATE markets
                       SET closed = TRUE, resolved_at = now(), updated_at = now(),
                           outcomes = %s, outcome_prices = %s
                     WHERE id = %s AND NOT closed
                    """,
                    (data.get("outcomes"), data.get("outcome_prices"), market_id),
                )
                resolved += cur.rowcount
        return resolved
