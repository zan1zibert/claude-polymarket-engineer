"""Integration tests for the signal service's schema + DB access.

The partial unique index and the settlement UPDATE are real SQL, so they are
tested against a live Postgres rather than mocked. Point a test DB at it (same
convention as tests/test_scorer_db.py):

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py

Without TEST_DATABASE_URL (or if unreachable) the whole module is skipped, so a
bare `pytest` stays green. Each test uses its own market ids and cleans up.
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from db import migrate
from lib.db import Db

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
        # FK order: positions -> signals -> belief_updates -> markets.
        # Positions and signals are cleared by the utest_ prefix rather than by
        # this module's ids, because position_aggregates sums the whole table.
        cur.execute("DELETE FROM paper_positions WHERE market_id LIKE 'utest\\_%'")
        cur.execute("DELETE FROM signals WHERE market_id LIKE 'utest\\_%'")
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


def _db():
    return Db(TEST_DATABASE_URL)


def _seed_belief_update(mid, article_url, *, ts):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO belief_updates
                (ts, market_id, market_title, previous_score, new_score,
                 article_url, reasoning)
            VALUES (%s, %s, %s, 0.5, 0.85, %s, 'because')
            """,
            (ts, mid, f"q {mid}", article_url),
        )


# --- market_for_signal ---------------------------------------------------

def test_market_for_signal_returns_none_for_unknown_market():
    assert _db().market_for_signal("utest_gZ_nope") is None


def test_market_for_signal_returns_the_newest_article():
    mid = "utest_gA"
    end = datetime.now(timezone.utc) + timedelta(days=5)
    _seed_market(mid, end_date=end)
    now = datetime.now(timezone.utc)
    _seed_belief_update(mid, "https://old.example", ts=now - timedelta(hours=2))
    _seed_belief_update(mid, "https://new.example", ts=now)

    row = _db().market_for_signal(mid)
    assert row["market_id"] == mid
    assert row["current_score"] == pytest.approx(0.85)
    assert row["closed"] is False
    assert row["article_url"] == "https://new.example"


def test_market_for_signal_article_is_none_when_never_evaluated():
    mid = "utest_gB"
    _seed_market(mid, end_date=datetime.now(timezone.utc) + timedelta(days=5))
    assert _db().market_for_signal(mid)["article_url"] is None


# --- signal_candidate_markets -------------------------------------------

_BANDS = dict(min_conviction_high=0.80, max_conviction_low=0.20, max_horizon_days=14)


def _candidate_ids():
    return {r["market_id"] for r in _db().signal_candidate_markets(**_BANDS)}


def test_candidates_include_both_conviction_bands():
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _seed_market("utest_gA", current_score=0.85, end_date=soon)
    _seed_market("utest_gB", current_score=0.15, end_date=soon)
    ids = _candidate_ids()
    assert {"utest_gA", "utest_gB"} <= ids


def test_candidates_exclude_mid_conviction():
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _seed_market("utest_gC", current_score=0.55, end_date=soon)
    assert "utest_gC" not in _candidate_ids()


def test_candidates_exclude_beyond_horizon_and_already_ended():
    _seed_market("utest_gA", current_score=0.85,
                 end_date=datetime.now(timezone.utc) + timedelta(days=40))
    _seed_market("utest_gB", current_score=0.85,
                 end_date=datetime.now(timezone.utc) - timedelta(days=1))
    ids = _candidate_ids()
    assert "utest_gA" not in ids
    assert "utest_gB" not in ids


def test_candidates_exclude_closed_markets():
    _seed_market("utest_gC", current_score=0.85, closed=True,
                 end_date=datetime.now(timezone.utc) + timedelta(days=5))
    assert "utest_gC" not in _candidate_ids()


def test_candidates_exclude_markets_with_an_open_position():
    mid = "utest_gD"
    _seed_market(mid, current_score=0.85,
                 end_date=datetime.now(timezone.utc) + timedelta(days=5))
    assert mid in _candidate_ids()
    _seed_position(mid, _seed_signal(mid))
    assert mid not in _candidate_ids()


# --- insert_signal + open_position --------------------------------------

def test_insert_signal_returns_an_id_and_open_position_uses_it():
    mid = "utest_gA"
    _seed_market(mid, end_date=datetime.now(timezone.utc) + timedelta(days=5))
    db = _db()
    sid = db.insert_signal(
        market_id=mid, market_title="q", rule="conviction_edge",
        source="belief_update", article_url="https://a.example", belief=0.85,
        yes_price=0.75, side="YES", cost_basis=0.75, edge=0.10, win_prob=0.85,
        expected_roi=0.1333, kelly=0.40, sharpe=0.28,
        end_date=datetime.now(timezone.utc) + timedelta(days=5), horizon_days=5.0,
    )
    assert isinstance(sid, int)
    assert db.open_position(
        signal_id=sid, market_id=mid, side="YES", entry_price=0.75, stake=1.0
    ) is True


def test_open_position_computes_shares_from_stake_and_price():
    mid = "utest_gB"
    _seed_market(mid)
    db = _db()
    sid = _seed_signal(mid)
    db.open_position(signal_id=sid, market_id=mid, side="NO", entry_price=0.25,
                     stake=1.0)
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT shares, status FROM paper_positions WHERE market_id = %s",
                    (mid,))
        shares, status = cur.fetchone()
    assert shares == pytest.approx(4.0)
    assert status == "open"


def test_open_position_returns_false_when_one_is_already_open():
    """The unique-index violation is a normal outcome, not an exception."""
    mid = "utest_gC"
    _seed_market(mid)
    db = _db()
    sid = _seed_signal(mid)
    assert db.open_position(signal_id=sid, market_id=mid, side="YES",
                            entry_price=0.75, stake=1.0) is True
    assert db.open_position(signal_id=sid, market_id=mid, side="YES",
                            entry_price=0.75, stake=1.0) is False


# --- settle_positions ---------------------------------------------------

def _settled_row(mid):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT status, exit_price, exit_reason, pnl, closed_at "
            "FROM paper_positions WHERE market_id = %s",
            (mid,),
        )
        return cur.fetchone()


@pytest.mark.parametrize(
    "side,outcome,expected_exit,expected_pnl",
    [
        ("YES", 1.0, 1.0, pytest.approx(1.0 / 0.5 - 1.0)),   # YES won:  +1.0
        ("YES", 0.0, 0.0, pytest.approx(-1.0)),              # YES lost: -stake
        ("NO", 0.0, 1.0, pytest.approx(1.0 / 0.5 - 1.0)),    # NO won:   +1.0
        ("NO", 1.0, 0.0, pytest.approx(-1.0)),               # NO lost:  -stake
    ],
)
def test_settlement_pnl_for_every_outcome(side, outcome, expected_exit, expected_pnl):
    mid = "utest_gA"
    _seed_market(mid, closed=True, resolved_outcome=outcome)
    _seed_position(mid, _seed_signal(mid, side=side), side=side, entry_price=0.5,
                   stake=1.0)

    settled = _db().settle_positions()
    assert [s["market_id"] for s in settled] == [mid]
    assert settled[0]["exit_price"] == pytest.approx(expected_exit)
    assert settled[0]["pnl"] == expected_pnl

    status, exit_price, exit_reason, pnl, closed_at = _settled_row(mid)
    assert status == "settled"
    assert exit_price == pytest.approx(expected_exit)
    assert exit_reason == "resolved"
    assert pnl == expected_pnl
    assert closed_at is not None


def test_settlement_skips_markets_without_a_known_outcome():
    """Closed but resolved_outcome NULL -> stays open, mirroring forecast_scores."""
    mid = "utest_gB"
    _seed_market(mid, closed=True, resolved_outcome=None)
    _seed_position(mid, _seed_signal(mid))
    assert _db().settle_positions() == []
    assert _settled_row(mid)[0] == "open"


def test_settlement_skips_an_ambiguous_half_outcome():
    """resolved_outcome = 0.5 means undetermined; the scorer excludes it too."""
    mid = "utest_gC"
    _seed_market(mid, closed=True, resolved_outcome=0.5)
    _seed_position(mid, _seed_signal(mid))
    assert _db().settle_positions() == []
    assert _settled_row(mid)[0] == "open"


def test_settlement_is_idempotent():
    mid = "utest_gD"
    _seed_market(mid, closed=True, resolved_outcome=1.0)
    _seed_position(mid, _seed_signal(mid), side="YES", entry_price=0.5)
    db = _db()
    assert len(db.settle_positions()) == 1
    assert db.settle_positions() == []          # second sweep books nothing twice
    assert _settled_row(mid)[3] == pytest.approx(1.0)


# --- position_aggregates ------------------------------------------------

def test_position_aggregates_over_a_mixed_book():
    win, loss, still_open = "utest_gA", "utest_gB", "utest_gC"
    _seed_market(win, closed=True, resolved_outcome=1.0)
    _seed_market(loss, closed=True, resolved_outcome=0.0)
    _seed_market(still_open)
    _seed_position(win, _seed_signal(win, side="YES"), side="YES", entry_price=0.5)
    _seed_position(loss, _seed_signal(loss, side="YES"), side="YES", entry_price=0.5)
    _seed_position(still_open, _seed_signal(still_open))

    db = _db()
    db.settle_positions()
    agg = db.position_aggregates()
    assert agg["open"] == 1
    assert agg["settled"] == 2
    assert agg["wins"] == 1
    assert agg["staked"] == pytest.approx(2.0)
    assert agg["pnl_total"] == pytest.approx(0.0)   # +1.0 and -1.0


def test_position_aggregates_on_an_empty_book():
    agg = _db().position_aggregates()
    assert agg["settled"] == 0
    assert agg["pnl_total"] == pytest.approx(0.0)
