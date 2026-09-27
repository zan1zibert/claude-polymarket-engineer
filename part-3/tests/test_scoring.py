"""Unit tests for lib/scoring.py — pure math, no infrastructure needed.

These always run under a bare `pytest`: the functions have no I/O, so we check
them against values that can be worked out by hand.
"""
from lib import scoring


def test_clamp01_bounds():
    assert scoring.clamp01(1.5) == 1.0
    assert scoring.clamp01(-0.2) == 0.0
    assert scoring.clamp01(0.42) == 0.42
    assert scoring.clamp01(1.0, eps=1e-6) == 1.0 - 1e-6
