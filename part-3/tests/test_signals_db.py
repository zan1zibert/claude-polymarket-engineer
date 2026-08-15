"""Integration tests for the signal service's schema + DB access.

The partial unique index and the settlement UPDATE are real SQL, so they are
tested against a live Postgres rather than mocked. Point a test DB at it (same
convention as tests/test_scorer_db.py):

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py

Without TEST_DATABASE_URL (or if unreachable) the whole module is skipped, so a
bare `pytest` stays green. Each test uses its own market ids and cleans up.
"""
import os

import psycopg
import pytest

from db import migrate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run signal DB tests"
)

_MARKET_IDS = ("utest_gA", "utest_gB", "utest_gC", "utest_gD")
_ZERO_VECTOR = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture(scope="module")
def _schema():
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


def _conn():
    return psycopg.connect(TEST_DATABASE_URL, autocommit=True)


def _cleanup():
    ids = list(_MARKET_IDS)
    with _conn() as c, c.cursor() as cur:
        # FK order: positions -> signals -> belief_updates -> markets
        cur.execute("DELETE FROM paper_positions WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM signals WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM belief_updates WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def _clean(_schema):
    _cleanup()
    yield
    _cleanup()


def _seed_market(mid, *, closed=False, current_score=0.85, end_date=None,
                 resolved_outcome=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO markets
                (id, question, current_score, seed_price, closed, resolved_outcome,
                 end_date, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (mid, f"q {mid}", current_score, 0.5, closed, resolved_outcome,
             end_date, _ZERO_VECTOR),
        )


def _seed_signal(mid, *, side="YES"):
    """Insert a minimal signals row and return its id."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals
                (market_id, market_title, rule, source, article_url, belief,
                 yes_price, side, cost_basis, edge, win_prob, expected_roi,
                 kelly, sharpe, end_date, horizon_days)
            VALUES (%s, %s, 'conviction_edge', 'sweep', NULL, 0.85, 0.75, %s,
                    0.75, 0.10, 0.85, 0.1333, 0.4, 0.28, NULL, 7.0)
            RETURNING id
            """,
            (mid, f"q {mid}", side),
        )
        return cur.fetchone()[0]


def _seed_position(mid, signal_id, *, side="YES", entry_price=0.75, stake=1.0,
                   status="open"):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_positions
                (signal_id, market_id, side, entry_price, stake, shares, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (signal_id, mid, side, entry_price, stake, stake / entry_price, status),
        )


def test_second_open_position_on_same_market_is_rejected():
    mid = "utest_gA"
    _seed_market(mid)
    sid = _seed_signal(mid)
    _seed_position(mid, sid)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _seed_position(mid, sid)


def test_a_settled_position_frees_the_market_for_re_entry():
    """The index is partial on status='open', so history never blocks re-entry."""
    mid = "utest_gB"
    _seed_market(mid)
    sid = _seed_signal(mid)
    _seed_position(mid, sid, status="settled")
    _seed_position(mid, sid, status="open")  # must not raise

    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM paper_positions WHERE market_id = %s", (mid,))
        assert cur.fetchone()[0] == 2
