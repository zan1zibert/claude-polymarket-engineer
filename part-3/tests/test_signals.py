"""Unit tests for the pure signal decision function.

Pure math, no I/O, so these always run (no TEST_DATABASE_URL needed).

The three rows from the design doc's theory table are asserted literally. They
are the worked examples the whole filter design was argued from, so if someone
"simplifies" the arithmetic later this fails loudly rather than silently
changing which bets the system takes.
"""
from datetime import datetime, timedelta, timezone

import pytest

from lib import signals

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

# Sentinel so a test can pass end_date=None explicitly and still get the default
# "5 days out" behaviour when it says nothing.
_MISSING = object()

THRESHOLDS = signals.Thresholds(
    min_edge=0.05,
    min_conviction_high=0.80,
    max_conviction_low=0.20,
    max_horizon_days=14,
    min_cost_basis=0.05,
    max_cost_basis=0.95,
)


def _evaluate(belief, yes_price, *, days_out=7.0, closed=False,
              thresholds=THRESHOLDS, end_date=_MISSING):
    end = NOW + timedelta(days=days_out) if end_date is _MISSING else end_date
    return signals.evaluate(
        belief=belief,
        yes_price=yes_price,
        end_date=end,
        now=NOW,
        closed=closed,
        thresholds=thresholds,
    )


# --- side selection -------------------------------------------------------

def test_belief_above_price_buys_yes():
    d = _evaluate(0.85, 0.75)
    assert d.fired
    assert d.side == "YES"
    assert d.cost_basis == pytest.approx(0.75)
    assert d.win_prob == pytest.approx(0.85)
    assert d.edge == pytest.approx(0.10)


def test_belief_below_price_buys_no():
    """High conviction does NOT mean we buy YES. Direction is sign(belief-price)."""
    d = _evaluate(0.80, 0.90)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.10)
    assert d.win_prob == pytest.approx(0.20)
    assert d.edge == pytest.approx(0.10)


def test_low_conviction_band_also_fires():
    d = _evaluate(0.15, 0.30)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.70)
    assert d.win_prob == pytest.approx(0.85)
    assert d.edge == pytest.approx(0.15)


# --- the theory table, asserted literally --------------------------------

def test_theory_row_cheap_longshot():
    """market 0.95, belief 0.85 -> NO at c=0.05: +200% ROI, 10.5% Kelly."""
    d = _evaluate(0.85, 0.95)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.05)
    assert d.win_prob == pytest.approx(0.15)
    assert d.edge == pytest.approx(0.10)
    assert d.expected_roi == pytest.approx(2.00, abs=1e-4)
    assert d.kelly == pytest.approx(0.10526, abs=1e-4)
    assert d.sharpe == pytest.approx(0.28005, abs=1e-4)


def test_theory_row_middling():
    """market 0.85, belief 0.80 -> NO at c=0.15: +33% ROI, 5.9% Kelly."""
    d = _evaluate(0.80, 0.85)
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.15)
    assert d.expected_roi == pytest.approx(0.33333, abs=1e-4)
    assert d.kelly == pytest.approx(0.05882, abs=1e-4)


def test_theory_row_favourite():
    """market 0.75, belief 0.80 -> YES at c=0.75: +6.7% ROI, 20% Kelly."""
    d = _evaluate(0.80, 0.75)
    assert d.side == "YES"
    assert d.cost_basis == pytest.approx(0.75)
    assert d.expected_roi == pytest.approx(0.06667, abs=1e-4)
    assert d.kelly == pytest.approx(0.20, abs=1e-4)


def test_favourite_has_lower_sharpe_than_the_big_edge_longshot():
    """The design's claim that tail bets win on risk-adjusted terms."""
    longshot = _evaluate(0.85, 0.95)
    favourite = _evaluate(0.80, 0.75)
    assert longshot.sharpe > 2 * favourite.sharpe


# --- gates ---------------------------------------------------------------

def test_closed_market_is_rejected():
    d = _evaluate(0.85, 0.75, closed=True)
    assert not d.fired
    assert d.reason == "market_closed"


def test_missing_belief_is_rejected():
    d = _evaluate(None, 0.75)
    assert not d.fired
    assert d.reason == "no_belief"


def test_missing_price_is_rejected():
    d = _evaluate(0.85, None)
    assert not d.fired
    assert d.reason == "no_price"


def test_missing_end_date_is_rejected():
    d = _evaluate(0.85, 0.75, end_date=None)
    assert not d.fired
    assert d.reason == "no_end_date"


def test_beyond_horizon_is_rejected():
    d = _evaluate(0.85, 0.75, days_out=30.0)
    assert not d.fired
    assert d.reason == "horizon"


def test_already_past_end_date_is_rejected():
    d = _evaluate(0.85, 0.75, days_out=-1.0)
    assert not d.fired
    assert d.reason == "horizon"


def test_mid_conviction_is_rejected():
    """The case the future market_overconfidence rule will pick up."""
    d = _evaluate(0.55, 0.15)
    assert not d.fired
    assert d.reason == "conviction"


def test_edge_below_threshold_is_rejected():
    d = _evaluate(0.85, 0.82)
    assert not d.fired
    assert d.reason == "min_edge"


def test_zero_edge_is_rejected():
    d = _evaluate(0.85, 0.85)
    assert not d.fired
    assert d.reason == "min_edge"


def test_cost_basis_below_band_is_rejected():
    """market 0.97, belief 0.85 -> NO at c=0.03, under the 0.05 floor."""
    d = _evaluate(0.85, 0.97)
    assert not d.fired
    assert d.reason == "cost_basis_band"


def test_tiny_cost_basis_on_the_yes_side_is_rejected():
    """Mirror case: market 0.02, belief 0.15 -> YES at c=0.02, under the floor.

    Note the max_cost_basis ceiling is defensive rather than currently reachable:
    c > 0.95 forces edge = q - c < 0.05, so min_edge rejects first while
    min_edge >= 0.05. It exists so raising min_edge later can't silently admit
    near-certain bets.
    """
    d = _evaluate(0.15, 0.02)
    assert not d.fired
    assert d.reason == "cost_basis_band"


def test_raising_min_cost_basis_removes_longshots():
    """min_cost_basis is the risk dial: it filters out cheap-side bets."""
    strict = signals.Thresholds(
        min_edge=0.05, min_conviction_high=0.80, max_conviction_low=0.20,
        max_horizon_days=14, min_cost_basis=0.30, max_cost_basis=0.95,
    )
    assert _evaluate(0.85, 0.95).fired                       # c=0.05, default band
    assert not _evaluate(0.85, 0.95, thresholds=strict).fired
    assert _evaluate(0.85, 0.95, thresholds=strict).reason == "cost_basis_band"
    assert _evaluate(0.80, 0.75, thresholds=strict).fired    # c=0.75 survives


# --- boundaries ----------------------------------------------------------

def test_edge_exactly_at_threshold_fires():
    d = _evaluate(0.85, 0.80)
    assert d.fired
    assert d.edge == pytest.approx(0.05)


def test_belief_exactly_at_conviction_boundary_fires():
    assert _evaluate(0.80, 0.70).fired
    assert _evaluate(0.20, 0.30).fired


def test_belief_just_inside_the_bands_is_rejected():
    assert _evaluate(0.79, 0.60).reason == "conviction"
    assert _evaluate(0.21, 0.40).reason == "conviction"


def test_end_date_exactly_at_horizon_fires():
    assert _evaluate(0.85, 0.75, days_out=14.0).fired


def test_cost_basis_exactly_on_the_floor_fires():
    """Both sides: the floor is inclusive, so c == min_cost_basis is allowed."""
    assert _evaluate(0.85, 0.95).fired    # NO  at c = 0.05 exactly
    assert _evaluate(0.15, 0.05).fired    # YES at c = 0.05 exactly


def test_cost_basis_exactly_on_a_tuned_floor_fires():
    """min_cost_basis is a config knob, not just the 0.05 default -- 0.20 is the
    value the design singles out as where the "market overconfidence" trade
    class disappears, so it must be reachable exactly, not off-by-a-ULP.

    1.0 - 0.80 == 0.19999999999999996 in IEEE-754, a hair under 0.20, so a
    literal `>= min_cost_basis` comparison would silently reject this
    legitimate boundary bet.
    """
    floor_20 = signals.Thresholds(
        min_edge=0.05, min_conviction_high=0.80, max_conviction_low=0.20,
        max_horizon_days=14, min_cost_basis=0.20, max_cost_basis=0.95,
    )
    d = _evaluate(0.15, 0.80, thresholds=floor_20)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.20)


def test_cost_basis_exactly_on_a_tuned_ceiling_fires():
    """Mirror case on the max_cost_basis end: 1.0 - 0.70 == 0.30000000000000004,
    a hair OVER 0.30, so a literal `<= max_cost_basis` comparison would
    silently reject this legitimate boundary bet too.
    """
    ceiling_30 = signals.Thresholds(
        min_edge=0.05, min_conviction_high=0.80, max_conviction_low=0.20,
        max_horizon_days=14, min_cost_basis=0.05, max_cost_basis=0.30,
    )
    d = _evaluate(0.05, 0.70, thresholds=ceiling_30)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.30)


# --- gate ordering -------------------------------------------------------

def test_first_failing_gate_wins():
    """Closed AND mid-conviction AND no edge -> reports market_closed."""
    d = _evaluate(0.55, 0.55, closed=True)
    assert d.reason == "market_closed"


def test_conviction_is_checked_before_edge():
    """Mid-conviction with a huge edge reports conviction, not min_edge."""
    d = _evaluate(0.55, 0.15)
    assert d.reason == "conviction"


def test_gate_order_pinned_through_missing_inputs_and_horizon():
    """Adjacent-pair pins for the no_belief -> no_price -> no_end_date -> horizon
    chain: only market_closed and conviction-before-edge were pinned before,
    leaving this chain free to silently reorder. Each case below violates two
    gates at once; the earlier gate's reason must always win, otherwise the
    signal_rejected_total{reason} metric would attribute the rejection to the
    wrong filter.
    """
    valid_end = NOW + timedelta(days=7)
    too_far_end = NOW + timedelta(days=30)

    # no_belief precedes no_price: both are missing.
    d = signals.evaluate(belief=None, yes_price=None, end_date=valid_end,
                         now=NOW, closed=False, thresholds=THRESHOLDS)
    assert d.reason == "no_belief"

    # no_price precedes no_end_date: both are missing.
    d = signals.evaluate(belief=0.85, yes_price=None, end_date=None,
                         now=NOW, closed=False, thresholds=THRESHOLDS)
    assert d.reason == "no_price"

    # no_end_date precedes horizon: end_date is missing outright, which a
    # horizon check running first would crash on (None arithmetic) rather
    # than merely misreport -- no_end_date must run first either way.
    d = signals.evaluate(belief=0.85, yes_price=0.75, end_date=None,
                         now=NOW, closed=False, thresholds=THRESHOLDS)
    assert d.reason == "no_end_date"

    # horizon precedes conviction: end_date out of range AND mid-conviction.
    d = signals.evaluate(belief=0.55, yes_price=0.55, end_date=too_far_end,
                         now=NOW, closed=False, thresholds=THRESHOLDS)
    assert d.reason == "horizon"


# --- degenerate inputs ---------------------------------------------------

def test_certain_belief_gives_finite_sharpe():
    """belief=1.0 would divide by sqrt(0); clamped instead of raising."""
    d = _evaluate(1.0, 0.60)
    assert d.fired
    assert d.sharpe == pytest.approx(0.4 / 1e-9 ** 0.5, rel=0.01)
    assert d.sharpe == d.sharpe  # not NaN


def test_rejected_decisions_carry_no_metrics():
    d = _evaluate(0.85, 0.82)
    assert d.expected_roi is None
    assert d.kelly is None
    assert d.sharpe is None
