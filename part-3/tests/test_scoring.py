"""Unit tests for lib/scoring.py — pure math, no infrastructure needed.

These always run under a bare `pytest`: the functions have no I/O, so we check
them against values that can be worked out by hand.
"""
from math import isclose, log

import pytest

from lib import scoring


# --------------------------------------------------------------- brier

def test_brier_perfect_forecast_is_zero():
    assert scoring.brier(1.0, 1.0) == 0.0
    assert scoring.brier(0.0, 0.0) == 0.0


def test_brier_coin_flip_is_quarter():
    # The canonical reference point: always saying 0.5 scores 0.25 either way.
    assert scoring.brier(0.5, 1.0) == 0.25
    assert scoring.brier(0.5, 0.0) == 0.25


def test_brier_confident_and_wrong_is_one():
    assert scoring.brier(0.0, 1.0) == 1.0
    assert scoring.brier(1.0, 0.0) == 1.0


# --------------------------------------------------------------- log_loss

def test_log_loss_matches_closed_form():
    # y=1, p=0.8  ->  -ln(0.8)
    assert isclose(scoring.log_loss(0.8, 1.0), -log(0.8))
    # y=0, p=0.3  ->  -ln(1-0.3)
    assert isclose(scoring.log_loss(0.3, 0.0), -log(0.7))


def test_log_loss_clamps_confident_wrong_to_finite():
    # p=0 with y=1 is infinite in the limit; clamping caps it at -ln(eps).
    loss = scoring.log_loss(0.0, 1.0, eps=1e-15)
    assert isclose(loss, -log(1e-15))
    assert loss < float("inf")


# --------------------------------------------------------------- clamp01

def test_clamp01_bounds():
    assert scoring.clamp01(1.5) == 1.0
    assert scoring.clamp01(-0.2) == 0.0
    assert scoring.clamp01(0.42) == 0.42
    assert scoring.clamp01(1.0, eps=1e-6) == 1.0 - 1e-6


# --------------------------------------------------------------- skill_score

def test_skill_positive_when_model_beats_reference():
    # belief mean 0.1 vs baseline 0.2  ->  1 - 0.5 = 0.5 (halved the error).
    assert isclose(scoring.skill_score(0.1, 0.2), 0.5)


def test_skill_zero_when_equal_and_negative_when_worse():
    assert scoring.skill_score(0.2, 0.2) == 0.0
    assert scoring.skill_score(0.3, 0.2) < 0.0


def test_skill_none_on_missing_or_zero_reference():
    assert scoring.skill_score(None, 0.2) is None
    assert scoring.skill_score(0.1, None) is None
    assert scoring.skill_score(0.1, 0.0) is None  # perfect baseline: undefined


# --------------------------------------------------------------- reliability_bins

def test_reliability_bins_length_and_empty_bins():
    bins = scoring.reliability_bins([(0.05, 0.0), (0.95, 1.0)], n_bins=10)
    assert len(bins) == 10
    # Only the first and last bins are populated; the rest are empty.
    assert bins[0][2] == 1 and bins[-1][2] == 1
    assert all(b == (None, None, 0) for b in bins[1:-1])


def test_reliability_bins_perfect_calibration_on_diagonal():
    # Two forecasts at 0.7: one YES, one NO -> observed frequency 0.5 in that bin.
    bins = scoring.reliability_bins([(0.7, 1.0), (0.7, 0.0)], n_bins=10)
    bin7 = bins[7]  # 0.7 falls in [0.7, 0.8)
    assert bin7[2] == 2
    assert isclose(bin7[0], 0.7)   # mean predicted
    assert isclose(bin7[1], 0.5)   # observed frequency


def test_reliability_bins_top_edge_folds_into_last_bin():
    # p == 1.0 must land in the last bin, not index out of range.
    bins = scoring.reliability_bins([(1.0, 1.0)], n_bins=10)
    assert bins[-1][2] == 1


def test_reliability_bins_rejects_bad_n():
    with pytest.raises(ValueError):
        scoring.reliability_bins([], n_bins=0)
