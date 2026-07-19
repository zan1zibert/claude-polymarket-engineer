"""Pure probabilistic-scoring functions — no I/O, no dependencies.

These grade a probabilistic forecast `p` (our belief that YES resolves, in
[0, 1]) against a realised binary outcome `y` (1.0 if YES won, 0.0 if NO won).

Everything here is a pure function of its arguments: no DB, no network, no
clock. That keeps it trivially unit-testable and lets the (future) backtest and
signal services reuse the exact same math the live scorer runs, so an offline
replay and the online scoreboard can never silently disagree.

Why these two rules: both Brier and log-loss are *strictly proper* (Gneiting &
Raftery 2007) — a forecaster minimises its expected score only by reporting its
true belief, so neither can be gamed by shading probabilities. They fail
differently on purpose:

  - Brier  = (p - y)**2         bounded [0, 1], forgiving of overconfidence.
                                 0 = perfect, 0.25 = always guessing 0.5, 1 =
                                 confidently wrong.
  - logloss = -[y ln p + (1-y) ln(1-p)]  the information-theoretic surprise;
                                 unbounded, punishes confident mistakes harshly
                                 (-> inf as p heads to the wrong extreme), which
                                 is exactly why it needs clamping.

Used together they catch both "wrong on average" (Brier) and "dangerously
overconfident" (log-loss).
"""
from math import log
from typing import Optional


def clamp01(p: float, eps: float = 0.0) -> float:
    """Clamp `p` into [eps, 1 - eps].

    With eps=0 this just guards against tiny out-of-range drift (a price of
    1.0000001). log_loss passes a small eps to keep ln() finite when a forecast
    sits exactly at 0 or 1 but the outcome went the other way.
    """
    lo, hi = eps, 1.0 - eps
    return lo if p < lo else hi if p > hi else p


def brier(p: float, y: float) -> float:
    """Brier score: squared error of a probability against a 0/1 outcome.

    0 is perfect; 0.25 is what you get by always predicting 0.5; 1 is being
    certain and wrong.
    """
    return (p - y) ** 2


def log_loss(p: float, y: float, eps: float = 1e-15) -> float:
    """Log loss (binary cross-entropy) for one forecast/outcome pair.

    `p` is clamped to [eps, 1 - eps] first: a perfectly-confident-but-wrong
    forecast (p=0 when y=1) has infinite log loss, which would poison any mean,
    so eps caps the per-observation penalty at a large but finite value.
    """
    p = clamp01(p, eps)
    return -(y * log(p) + (1.0 - y) * log(1.0 - p))


def skill_score(mean_model: Optional[float], mean_reference: Optional[float]) -> Optional[float]:
    """Skill score of a model mean-score against a reference mean-score.

    skill = 1 - model / reference. Positive => the model beats the reference
    (lower error); 0 => it matches it; negative => the reference is better and
    the model is adding noise. This is the headline "are we beating the market?"
    number when `mean_model` is our belief's mean Brier and `mean_reference` is
    the market baseline's. Returns None if either input is missing or the
    reference is ~0 (a perfect baseline leaves no room to improve and the ratio
    is undefined).
    """
    if mean_model is None or mean_reference is None or mean_reference <= 0.0:
        return None
    return 1.0 - mean_model / mean_reference


def reliability_bins(
    pairs: list[tuple[float, float]], n_bins: int = 10
) -> list[tuple[Optional[float], Optional[float], int]]:
    """Group forecasts into equal-width probability bins for a reliability curve.

    `pairs` is [(p, y), ...]. Splits [0, 1] into `n_bins` equal-width buckets and
    returns one entry per bin, in ascending order:

        (mean_predicted, observed_frequency, count)

    `mean_predicted` is the average forecast in the bin; `observed_frequency` is
    the fraction that actually resolved YES. A perfectly calibrated forecaster
    has mean_predicted == observed_frequency in every bin (the diagonal). Empty
    bins return (None, None, 0) so the returned list always has length `n_bins`
    and lines up with fixed x-axis positions on a plot.
    """
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")

    sums_p = [0.0] * n_bins
    sums_y = [0.0] * n_bins
    counts = [0] * n_bins
    for p, y in pairs:
        p = clamp01(p)
        # p == 1.0 would index n_bins (out of range); the min() folds the top
        # edge into the last bin so [0, 1] maps cleanly onto 0..n_bins-1.
        idx = min(int(p * n_bins), n_bins - 1)
        sums_p[idx] += p
        sums_y[idx] += y
        counts[idx] += 1

    out: list[tuple[Optional[float], Optional[float], int]] = []
    for i in range(n_bins):
        if counts[i] == 0:
            out.append((None, None, 0))
        else:
            out.append((sums_p[i] / counts[i], sums_y[i] / counts[i], counts[i]))
    return out
