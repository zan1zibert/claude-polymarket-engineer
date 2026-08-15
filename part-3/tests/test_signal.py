"""Unit tests for the signal service's orchestration, with fakes for DB + Gamma.

The decision math is covered in tests/test_signals.py and the SQL in
tests/test_signals_db.py; what is left here is the wiring — that a fired signal
writes a row AND a position, that an already-held market writes the row but not a
position, that a rejection writes neither, and that the sweep settles before it
rescans.
"""
from datetime import datetime, timedelta, timezone

import pytest

from lib.config import load_settings
from services.signal import main as signal_service

NOW = datetime.now(timezone.utc)


def _settings(**overrides):
    import dataclasses
    return dataclasses.replace(load_settings(), **overrides)


class _FakeDb:
    def __init__(self, markets, *, open_markets=(), settle=()):
        self._markets = markets
        self._open = set(open_markets)
        self._settle = list(settle)
        self.signals = []
        self.positions = []
        self.settled_calls = 0

    def market_for_signal(self, market_id):
        return self._markets.get(market_id)

    def signal_candidate_markets(self, **_kwargs):
        return [m for mid, m in self._markets.items() if mid not in self._open]

    def insert_signal(self, **kwargs):
        self.signals.append(kwargs)
        return len(self.signals)

    def open_position(self, **kwargs):
        if kwargs["market_id"] in self._open:
            return False
        self._open.add(kwargs["market_id"])
        self.positions.append(kwargs)
        return True

    def settle_positions(self):
        self.settled_calls += 1
        out, self._settle = self._settle, []
        return out

    def position_aggregates(self):
        return {"open": len(self._open), "settled": 0, "pnl_total": 0.0,
                "wins": 0, "staked": 0.0}


def _market(mid, *, score=0.85, days_out=5.0, closed=False, article=None):
    return {
        "market_id": mid,
        "question": f"q {mid}",
        "current_score": score,
        "end_date": NOW + timedelta(days=days_out),
        "closed": closed,
        "article_url": article,
    }


def _prices(mapping):
    """Stand in for polymarket.fetch_statuses."""
    def _fetch(_client, ids, **_kwargs):
        return {i: {"yes_price": mapping.get(i), "closed": False,
                    "end_date": None, "resolved_outcome": None}
                for i in ids if i in mapping}
    return _fetch


# --- evaluate_market ----------------------------------------------------

def test_fired_signal_writes_a_row_and_a_position():
    db = _FakeDb({})   # evaluate_market takes the market directly, not by id
    outcome = signal_service.evaluate_market(
        db, _market("m1", article="https://a.example"), 0.75,
        _settings(), source="belief_update",
    )
    assert outcome == "fired"
    assert len(db.signals) == 1
    assert len(db.positions) == 1

    sig = db.signals[0]
    assert sig["side"] == "YES"
    assert sig["source"] == "belief_update"
    assert sig["rule"] == "conviction_edge"
    assert sig["article_url"] == "https://a.example"
    assert sig["cost_basis"] == pytest.approx(0.75)

    pos = db.positions[0]
    assert pos["signal_id"] == 1
    assert pos["entry_price"] == pytest.approx(0.75)
    assert pos["stake"] == pytest.approx(1.0)


def test_position_entry_price_is_the_cost_basis_not_the_yes_price():
    """A NO position is entered at 1 - yes_price, not at yes_price."""
    db = _FakeDb({})
    signal_service.evaluate_market(
        db, _market("m1", score=0.80), 0.90, _settings(), source="sweep"
    )
    assert db.signals[0]["side"] == "NO"
    assert db.positions[0]["side"] == "NO"
    assert db.positions[0]["entry_price"] == pytest.approx(0.10)


def test_already_held_market_records_the_signal_but_opens_nothing():
    db = _FakeDb({}, open_markets=["m1"])
    outcome = signal_service.evaluate_market(
        db, _market("m1"), 0.75, _settings(), source="sweep"
    )
    assert outcome == "position_open"
    assert len(db.signals) == 1     # intent is still recorded
    assert db.positions == []       # exposure is not doubled


def test_rejected_candidate_writes_nothing():
    db = _FakeDb({})
    outcome = signal_service.evaluate_market(
        db, _market("m1", score=0.55), 0.50, _settings(), source="sweep"
    )
    assert outcome == "conviction"
    assert db.signals == []
    assert db.positions == []


def test_missing_price_is_rejected_as_no_price():
    db = _FakeDb({})
    assert signal_service.evaluate_market(
        db, _market("m1"), None, _settings(), source="sweep"
    ) == "no_price"


def test_thresholds_are_taken_from_settings():
    db = _FakeDb({})
    strict = _settings(signal_min_cost_basis=0.30)
    assert signal_service.evaluate_market(
        db, _market("m1", score=0.85), 0.95, strict, source="sweep"
    ) == "cost_basis_band"
    assert signal_service.evaluate_market(
        db, _market("m1", score=0.85), 0.95, _settings(), source="sweep"
    ) == "fired"


# --- sweep_once ---------------------------------------------------------

def test_sweep_settles_then_rescans(monkeypatch):
    db = _FakeDb(
        {"m1": _market("m1"), "m2": _market("m2", score=0.55)},
        settle=[{"market_id": "m0", "side": "YES", "exit_price": 1.0, "pnl": 1.0}],
    )
    monkeypatch.setattr(signal_service.polymarket, "fetch_statuses",
                        _prices({"m1": 0.75, "m2": 0.50}))

    sweep = signal_service.sweep_once(db, object(), _settings())
    assert db.settled_calls == 1
    assert sweep["settled"] == 1
    assert sweep["evaluated"] == 2
    assert sweep["fired"] == 1
    assert [s["source"] for s in db.signals] == ["sweep"]


def test_sweep_with_no_candidates_makes_no_gamma_call(monkeypatch):
    db = _FakeDb({})
    called = []

    def _boom(*_a, **_k):
        called.append(1)
        return {}

    monkeypatch.setattr(signal_service.polymarket, "fetch_statuses", _boom)
    assert signal_service.sweep_once(db, object(), _settings())["evaluated"] == 0
    assert called == []


def test_settlement_completes_even_when_the_price_fetch_fails(monkeypatch):
    """Settlement runs first, so a Gamma outage cannot delay booking known P&L.

    sweep_once lets the error propagate; run()'s try/except logs it and the next
    cycle retries the rescan.
    """
    db = _FakeDb({"m1": _market("m1")},
                 settle=[{"market_id": "m0", "side": "YES", "exit_price": 1.0,
                          "pnl": 1.0}])

    def _raise(*_a, **_k):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(signal_service.polymarket, "fetch_statuses", _raise)
    with pytest.raises(RuntimeError):
        signal_service.sweep_once(db, object(), _settings())
    assert db.settled_calls == 1
