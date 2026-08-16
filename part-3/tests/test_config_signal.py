"""The signal service's knobs: defaults, and that each env var is actually read.

Defaults are load-bearing here — they are the filter, and the design doc argues
for these specific numbers — so they are asserted rather than assumed.
"""
import pytest

from lib.config import load_settings

_ENV = {
    "SIGNAL_MIN_EDGE": ("signal_min_edge", "0.09", 0.09, 0.05),
    "SIGNAL_MIN_CONVICTION_HIGH": ("signal_min_conviction_high", "0.9", 0.9, 0.80),
    "SIGNAL_MAX_CONVICTION_LOW": ("signal_max_conviction_low", "0.1", 0.1, 0.20),
    "SIGNAL_MAX_HORIZON_DAYS": ("signal_max_horizon_days", "30", 30, 14),
    "SIGNAL_MIN_COST_BASIS": ("signal_min_cost_basis", "0.3", 0.3, 0.05),
    "SIGNAL_MAX_COST_BASIS": ("signal_max_cost_basis", "0.9", 0.9, 0.95),
    "SIGNAL_STAKE": ("signal_stake", "5", 5.0, 1.0),
    "SIGNAL_SWEEP_INTERVAL_SECONDS": ("signal_sweep_interval_seconds", "60", 60, 3600),
}


@pytest.mark.parametrize("env_var", sorted(_ENV))
def test_default_when_unset(monkeypatch, env_var):
    field, _raw, _override, default = _ENV[env_var]
    monkeypatch.delenv(env_var, raising=False)
    assert getattr(load_settings(), field) == pytest.approx(default)


@pytest.mark.parametrize("env_var", sorted(_ENV))
def test_env_var_is_read(monkeypatch, env_var):
    field, raw, override, _default = _ENV[env_var]
    monkeypatch.setenv(env_var, raw)
    assert getattr(load_settings(), field) == pytest.approx(override)


def test_belief_dirty_key_default():
    assert load_settings().belief_dirty_key == "belief_dirty"
