"""Integration tests for the relevance-check DB layer: retrieval-only
top_k_markets (no distance filter) and log_relevance_check.

Point a test DB at these with:

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_relevance_checks_db.py

Without TEST_DATABASE_URL (or if the DB is unreachable) the whole module is
skipped, so a bare `pytest` stays green with no infrastructure.
"""
import os

import psycopg
import pytest

from db import migrate
from lib.db import Db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run relevance-check DB tests"
)

_MARKET_IDS = ("utest_rcA", "utest_rcB")


def _unit_vector(index: int, dim: int = 1024) -> str:
    """A pgvector literal with a 1 at `index` and 0 elsewhere (a well-defined,
    nonzero vector — needed because cosine distance from the zero vector is
    undefined)."""
    values = ["0"] * dim
    values[index] = "1"
    return "[" + ",".join(values) + "]"


@pytest.fixture(scope="module")
def _schema():
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


@pytest.fixture
def db(_schema):
    def _cleanup():
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
            cur.execute("DELETE FROM relevance_checks WHERE market_id = ANY(%s)", (list(_MARKET_IDS),))
            cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (list(_MARKET_IDS),))

    _cleanup()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        # A (index 0) sits at distance 0 from the query; B (index 1) is
        # orthogonal to it — cosine distance 1.0, well beyond the old 0.35
        # threshold. Both must still come back once filtering is Groq's job.
        cur.execute(
            "INSERT INTO markets (id, question, embedding) VALUES (%s, %s, %s)",
            (_MARKET_IDS[0], "question A", _unit_vector(0)),
        )
        cur.execute(
            "INSERT INTO markets (id, question, embedding) VALUES (%s, %s, %s)",
            (_MARKET_IDS[1], "question B", _unit_vector(1)),
        )
    yield Db(TEST_DATABASE_URL)
    _cleanup()


def test_top_k_markets_has_no_distance_filter(db):
    query_embedding = [1.0] + [0.0] * 1023  # matches market A exactly, orthogonal to B

    results = db.top_k_markets(query_embedding, k=2)

    assert {m.id for m in results} == set(_MARKET_IDS)


def test_top_k_markets_respects_k(db):
    query_embedding = [1.0] + [0.0] * 1023

    results = db.top_k_markets(query_embedding, k=1)

    assert len(results) == 1
    assert results[0].id == _MARKET_IDS[0]  # the exact match, nearest by distance


def test_log_relevance_check_inserts_a_row(db):
    db.log_relevance_check(
        article_url="https://example.com/a",
        article_title="Some Article",
        market_id=_MARKET_IDS[0],
        relevant=True,
        reasoning="same event",
        model="llama-3.1-8b-instant",
    )

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "SELECT article_url, article_title, market_id, relevant, reasoning, model "
            "FROM relevance_checks WHERE market_id = %s",
            (_MARKET_IDS[0],),
        )
        row = cur.fetchone()

    assert row == (
        "https://example.com/a", "Some Article", _MARKET_IDS[0], True, "same event",
        "llama-3.1-8b-instant",
    )
