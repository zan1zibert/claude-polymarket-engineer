"""Pure signal-decision math — no I/O, no clock, no dependencies.

Given our belief and the live market price, decide whether to bet, which side,
and how good the bet is. Everything here is a pure function of its arguments —
`now` is a parameter, never `datetime.now()` — for the same reason lib/scoring.py
is: a future backtest must be able to replay the identical decisions offline, and
an online/offline disagreement in *this* file would invalidate every conclusion
drawn from the paper P&L.

## The math

Side selection happens first; everything else is expressed in terms of the side
we would buy:

    side = YES if belief > price else NO
    c    = price  if YES else 1 - price      # what one share costs
    q    = belief if YES else 1 - belief     # our P(this side wins)
    edge = q - c                             # positive by construction

Three quantities follow, and with a flat stake they are the whole basis of the
risk/reward tradeoff:

    expected_roi = edge / c            reward per euro staked
    kelly        = edge / (1 - c)      fraction of bankroll this bet deserves
    sharpe       = edge / sqrt(q(1-q)) reward per unit of variance

ROI and Kelly point in OPPOSITE directions in c, which is why `min_cost_basis` is
the system's risk dial rather than an arbitrary sanity bound: c < 0.5 means we are
buying the underdog, so raising the floor trades ROI for hit rate. Worked
examples, asserted in tests/test_signals.py:

    market 0.95, belief 0.85 -> NO  at c=0.05: ROI +200%, Kelly 10.5%, wins 15%
    market 0.85, belief 0.80 -> NO  at c=0.15: ROI  +33%, Kelly  5.9%, wins 20%
    market 0.75, belief 0.80 -> YES at c=0.75: ROI  +6.7%, Kelly 20.0%, wins 80%

## What the conviction band does NOT do

It does not pick a direction. It only decides whether our belief is confident
enough to act on at all; direction is sign(belief - price). So a high-conviction
0.80 belief against a market at 0.85 buys NO — betting against our own
directional view because the market is *more* extreme than we are. That is
correct by expected value and intended; `Decision.side` records it so a log
reader is never left guessing.
"""
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Optional

from lib.scoring import clamp01

RULE_CONVICTION_EDGE = "conviction_edge"

# Keeps sqrt(q(1-q)) finite when a belief sits exactly at 0 or 1. Such a belief is
# already pathological; the point is to produce a very large Sharpe rather than
# raise ZeroDivisionError inside a service loop.
_SHARPE_EPS = 1e-9

# Binary floats can't represent 0.80, 0.85, 0.15, etc. exactly, so an edge that is
# conceptually exactly at min_edge (e.g. belief=0.85, price=0.80) can land a few
# ULPs below it (0.04999999999999993 rather than 0.05). Without this tolerance the
# `min_edge` gate would reject the "exactly at threshold" boundary case the design
# doc and tests treat as inclusive. It is intentionally far smaller than any
# threshold value in Thresholds, so it never masks a real edge shortfall.
_EDGE_EPS = 1e-9

SIDE_YES = "YES"
SIDE_NO = "NO"


@dataclass(frozen=True)
class Thresholds:
    """The filter configuration, straight from lib.config.Settings."""
    min_edge: float
    min_conviction_high: float
    max_conviction_low: float
    max_horizon_days: float
    min_cost_basis: float
    max_cost_basis: float


@dataclass(frozen=True)
class Decision:
    """Either a fired signal with every number it was made from, or a rejection.

    On a rejection only `reason` is set — the metric fields stay None, so a caller
    can never accidentally persist half-computed arithmetic.
    """
    fired: bool
    reason: Optional[str] = None
    side: Optional[str] = None
    cost_basis: Optional[float] = None
    win_prob: Optional[float] = None
    edge: Optional[float] = None
    expected_roi: Optional[float] = None
    kelly: Optional[float] = None
    sharpe: Optional[float] = None
    horizon_days: Optional[float] = None


def _reject(reason: str, *, horizon_days: Optional[float] = None) -> Decision:
    return Decision(fired=False, reason=reason, horizon_days=horizon_days)


def evaluate(
    *,
    belief: Optional[float],
    yes_price: Optional[float],
    end_date: Optional[datetime],
    now: datetime,
    closed: bool,
    thresholds: Thresholds,
) -> Decision:
    """Decide whether this market is worth a bet right now.

    `end_date` and `now` must both be timezone-aware (Postgres hands back
    TIMESTAMPTZ). Gates run in a fixed order and the FIRST failure is the reason,
    so a rejection reason is always the cheapest true explanation — that ordering
    is what makes the `signal_rejected_total{reason}` counter readable.

    Note what is absent: this function knows nothing about open positions. A
    signal can legitimately fire on a market we already hold (the signals row is
    still worth recording); deciding whether to take exposure is the service's
    job, not the math's.
    """
    if closed:
        return _reject("market_closed")
    if belief is None:
        return _reject("no_belief")
    if yes_price is None:
        return _reject("no_price")
    if end_date is None:
        return _reject("no_end_date")

    horizon_days = (end_date - now).total_seconds() / 86400.0
    if not (0.0 < horizon_days <= thresholds.max_horizon_days):
        return _reject("horizon", horizon_days=horizon_days)

    if not (belief >= thresholds.min_conviction_high
            or belief <= thresholds.max_conviction_low):
        return _reject("conviction", horizon_days=horizon_days)

    # Side, then everything in terms of the side we would buy.
    if belief > yes_price:
        side, cost_basis, win_prob = SIDE_YES, yes_price, belief
    else:
        side, cost_basis, win_prob = SIDE_NO, 1.0 - yes_price, 1.0 - belief
    edge = win_prob - cost_basis

    if edge < thresholds.min_edge - _EDGE_EPS:
        return _reject("min_edge", horizon_days=horizon_days)
    if not (thresholds.min_cost_basis <= cost_basis <= thresholds.max_cost_basis):
        return _reject("cost_basis_band", horizon_days=horizon_days)

    # Derived metrics are computed only after the band gate, which is what makes
    # both divisions safe: the gate guarantees 0 < min_cost_basis <= c <=
    # max_cost_basis < 1, so neither c nor (1 - c) can be zero.
    variance = win_prob * (1.0 - win_prob)
    return Decision(
        fired=True,
        side=side,
        cost_basis=cost_basis,
        win_prob=win_prob,
        edge=edge,
        expected_roi=edge / cost_basis,
        kelly=edge / (1.0 - cost_basis),
        sharpe=edge / sqrt(clamp01(variance, _SHARPE_EPS)),
        horizon_days=horizon_days,
    )
