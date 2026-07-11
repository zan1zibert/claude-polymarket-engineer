"""Integration tests for the scorer's DB access + one full score_once cycle.

resolved_unscored_markets is an anti-join and score_aggregates is real aggregate
SQL, so both are tested against a live Postgres rather than mocked. Point a test
DB at it (same convention as test_db.py):

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_scorer_db.py

Without TEST_DATABASE_URL (or if unreachable) the whole module is skipped, so a
bare `pytest` stays green. Each test uses its own market ids and cleans up.
"""
import os

import psycopg
import pytest

from db import migrate
from lib.db import Db
from services.scorer import main as scorer

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run scorer DB tests"
)

_MARKET_IDS = ("utest_sA", "utest_sB", "utest_sC")
_ZERO_VECTOR = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture(scope="module")
def _schema():
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


def _cleanup():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DELETE FROM forecast_scores WHERE market_id = ANY(%s)", (list(_MARKET_IDS),))
        cur.execute("DELETE FROM belief_updates WHERE market_id = ANY(%s)", (list(_MARKET_IDS),))
        cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (list(_MARKET_IDS),))


def _seed_market(mid, *, closed, current_score, seed_price, resolved_outcome, n_updates=0):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO markets
                (id, question, current_score, seed_price, closed, resolved_outcome, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (mid, f"q {mid}", current_score, seed_price, closed, resolved_outcome, _ZERO_VECTOR),
        )
        for i in range(n_updates):
            cur.execute(
                """
                INSERT INTO belief_updates
                    (market_id, market_title, previous_score, new_score, article_url, reasoning)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (mid, f"q {mid}", None, current_score, f"http://x/{mid}/{i}", "r"),
            )


@pytest.fixture
def db(_schema):
    _cleanup()
    yield Db(TEST_DATABASE_URL)
    _cleanup()


def test_unscored_queue_excludes_open_and_outcomeless(db):
    _seed_market("utest_sA", closed=True, current_score=0.8, seed_price=0.6,
                 resolved_outcome=1.0, n_updates=2)
    _seed_market("utest_sB", closed=False, current_score=0.5, seed_price=0.5,
                 resolved_outcome=None)                       # still open
    _seed_market("utest_sC", closed=True, current_score=0.5, seed_price=0.5,
                 resolved_outcome=None)                       # closed, outcome unknown

    pending = db.resolved_unscored_markets()
    ids = {p["market_id"] for p in pending}
    assert ids == {"utest_sA"}
    row = next(p for p in pending if p["market_id"] == "utest_sA")
    assert row["outcome"] == 1.0
    assert row["final_belief"] == 0.8
    assert row["seed_price"] == 0.6
    assert row["n_updates"] == 2


def test_corpus_counts_buckets_by_state(db):
    # corpus_counts scans the whole table, so assert on deltas from a baseline
    # rather than absolute counts (other rows may exist in the test DB).
    before = db.corpus_counts()
    _seed_market("utest_sA", closed=False, current_score=0.5, seed_price=0.5,
                 resolved_outcome=None)                       # open
    _seed_market("utest_sB", closed=True, current_score=0.8, seed_price=0.6,
                 resolved_outcome=1.0)                        # closed + graded
    _seed_market("utest_sC", closed=True, current_score=0.5, seed_price=0.5,
                 resolved_outcome=None)                       # closed, awaiting outcome

    after = db.corpus_counts()
    assert after["open"] - before["open"] == 1
    assert after["closed"] - before["closed"] == 2            # both closed rows
    assert after["awaiting"] - before["awaiting"] == 1        # only the outcomeless one


def test_insert_score_is_idempotent_and_removes_from_queue(db):
    _seed_market("utest_sA", closed=True, current_score=0.8, seed_price=0.6,
                 resolved_outcome=1.0)
    first = db.insert_score("utest_sA", outcome=1.0, final_belief=0.8, seed_price=0.6,
                            n_updates=0, brier_belief=0.04, logloss_belief=0.22,
                            brier_baseline=0.16, logloss_baseline=0.51)
    second = db.insert_score("utest_sA", outcome=1.0, final_belief=0.8, seed_price=0.6,
                             n_updates=0, brier_belief=0.04, logloss_belief=0.22,
                             brier_baseline=0.16, logloss_baseline=0.51)
    assert first is True and second is False          # ON CONFLICT DO NOTHING
    assert db.resolved_unscored_markets() == []       # now out of the queue


def test_score_once_grades_and_computes_skill(db):
    # Belief nails it (0.9 on a YES), baseline was lukewarm (0.6) -> positive skill.
    _seed_market("utest_sA", closed=True, current_score=0.9, seed_price=0.6,
                 resolved_outcome=1.0, n_updates=1)
    # Market with no baseline captured -> baseline columns stay NULL.
    _seed_market("utest_sB", closed=True, current_score=0.2, seed_price=None,
                 resolved_outcome=0.0)

    result = scorer.score_once(db)
    assert result["scored"] == 2
    assert result["skill"] is not None and result["skill"] > 0.0

    with psycopg.connect(TEST_DATABASE_URL) as c, c.cursor() as cur:
        cur.execute(
            "SELECT brier_belief, brier_baseline FROM forecast_scores WHERE market_id = %s",
            ("utest_sA",),
        )
        brier_belief, brier_baseline = cur.fetchone()
        assert abs(brier_belief - 0.01) < 1e-9        # (0.9-1)^2
        assert abs(brier_baseline - 0.16) < 1e-9      # (0.6-1)^2

        cur.execute("SELECT brier_baseline FROM forecast_scores WHERE market_id = %s", ("utest_sB",))
        assert cur.fetchone()[0] is None              # no seed_price -> NULL baseline

    # Second run has nothing new to do.
    assert scorer.score_once(db)["scored"] == 0
