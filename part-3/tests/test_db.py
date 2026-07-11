"""Integration tests for Db.record_prices (the price-series writer).

record_prices runs real SQL (a DISTINCT ON latest-price read + a batched insert),
so it's tested against a real Postgres rather than a mock. Point a test DB at it:

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_db.py

Without TEST_DATABASE_URL (or if the DB is unreachable) the whole module is
skipped, so a bare `pytest` stays green with no infrastructure. The fixture
applies migrations (idempotent) so the schema is present, and each test uses its
own market ids and cleans up after itself.
"""
import os

import psycopg
import pytest

from db import migrate
from lib.db import Db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run price-series DB tests"
)

_MARKET_IDS = ("utest_mA", "utest_mB")
_ZERO_VECTOR = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture(scope="module")
def _schema():
    """Ensure the schema exists (migrations are idempotent) before any test runs."""
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:  # unreachable DB → skip rather than error the suite
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


@pytest.fixture
def db(_schema):
    """A Db plus clean market_prices/markets rows for the test ids, torn down after."""
    def _cleanup():
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
            cur.execute("DELETE FROM market_prices WHERE market_id = ANY(%s)", (list(_MARKET_IDS),))
            cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (list(_MARKET_IDS),))

    _cleanup()
    # market_prices has an FK to markets(id); seed the parent rows.
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        for mid in _MARKET_IDS:
            cur.execute(
                "INSERT INTO markets (id, question, embedding) VALUES (%s, %s, %s)",
                (mid, f"question {mid}", _ZERO_VECTOR),
            )
    yield Db(TEST_DATABASE_URL)
    _cleanup()


def _counts(url):
    with psycopg.connect(url) as c, c.cursor() as cur:
        cur.execute(
            "SELECT market_id, count(*) FROM market_prices WHERE market_id = ANY(%s) GROUP BY market_id",
            (list(_MARKET_IDS),),
        )
        return dict(cur.fetchall())


def test_first_observation_always_written(db):
    written = db.record_prices([("utest_mA", 0.40), ("utest_mB", 0.60)], min_change=0.005)
    assert written == 2
    assert _counts(TEST_DATABASE_URL) == {"utest_mA": 1, "utest_mB": 1}


def test_unchanged_and_subthreshold_moves_skipped(db):
    db.record_prices([("utest_mA", 0.40), ("utest_mB", 0.60)], min_change=0.005)
    # mA identical, mB moved 0.002 (< epsilon) → neither written.
    written = db.record_prices([("utest_mA", 0.40), ("utest_mB", 0.602)], min_change=0.005)
    assert written == 0
    assert _counts(TEST_DATABASE_URL) == {"utest_mA": 1, "utest_mB": 1}


def test_moves_at_or_above_threshold_written(db):
    db.record_prices([("utest_mA", 0.40), ("utest_mB", 0.60)], min_change=0.005)
    # mA moved exactly 0.02, mB moved 0.01 → both >= epsilon.
    written = db.record_prices([("utest_mA", 0.42), ("utest_mB", 0.61)], min_change=0.005)
    assert written == 2
    assert _counts(TEST_DATABASE_URL) == {"utest_mA": 2, "utest_mB": 2}


def test_empty_batch_is_noop(db):
    assert db.record_prices([], min_change=0.005) == 0
    assert _counts(TEST_DATABASE_URL) == {}


def test_min_change_zero_records_every_observation(db):
    # min_change=0.0 disables dedupe: abs(diff) >= 0 is always true, so even an
    # identical repeat is written. This is the "record every point" mode.
    db.record_prices([("utest_mA", 0.40)], min_change=0.0)
    written = db.record_prices([("utest_mA", 0.40)], min_change=0.0)
    assert written == 1
    assert _counts(TEST_DATABASE_URL) == {"utest_mA": 2}


def test_batch_shares_one_snapshot_timestamp(db):
    db.record_prices([("utest_mA", 0.40), ("utest_mB", 0.60)], min_change=0.005)
    with psycopg.connect(TEST_DATABASE_URL) as c, c.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT ts) FROM market_prices WHERE market_id = ANY(%s)",
            (list(_MARKET_IDS),),
        )
        # Both rows written in one call → one coherent snapshot timestamp.
        assert cur.fetchone()[0] == 1
