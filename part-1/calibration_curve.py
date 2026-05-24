"""
calibration_curve.py — plot predicted probability vs actual outcome frequency.

Usage:
    python calibration_curve.py outputs/scores_results_<timestamp>.csv
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


BINS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def calibration_points(probs: pd.Series, outcomes: pd.Series):
    """Return (bin_midpoint, actual_frequency, count) for non-empty bins."""
    midpoints, frequencies, counts = [], [], []
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        mask = (probs >= lo) & (probs < hi)
        # include 1.0 in the last bin
        if hi == 1.0:
            mask = (probs >= lo) & (probs <= hi)
        n = mask.sum()
        if n > 0:
            midpoints.append((lo + hi) / 2)
            frequencies.append(outcomes[mask].mean())
            counts.append(n)
    return np.array(midpoints), np.array(frequencies), np.array(counts)


def main(scores_csv: str):
    df = pd.read_csv(scores_csv)

    required = {"claude_prob", "market_prob", "yes_won"}
    if not required.issubset(df.columns):
        print(f"ERROR: CSV must have columns: {required}")
        sys.exit(1)

    df = df[df["yes_won"].notna()].copy()
    df["yes_won"] = df["yes_won"].astype(float)
    n = len(df)

    claude_x, claude_y, claude_n = calibration_points(df["claude_prob"], df["yes_won"])
    market_x, market_y, market_n = calibration_points(df["market_prob"], df["yes_won"])

    fig, ax = plt.subplots(figsize=(7, 6))

    # perfect calibration diagonal
    ax.plot([0, 1], [0, 1], "--", color="grey", linewidth=1, label="Perfect calibration")

    # Claude
    ax.plot(claude_x, claude_y, "o-", color="#2563eb", linewidth=2,
            markersize=7, label="Claude")
    for x, y, c in zip(claude_x, claude_y, claude_n):
        ax.annotate(f"n={c}", (x, y), textcoords="offset points",
                    xytext=(5, 5), fontsize=7, color="#2563eb")

    # Market price
    ax.plot(market_x, market_y, "s-", color="#dc2626", linewidth=2,
            markersize=7, label="Market price")

    ax.set_xlabel("Predicted probability", fontsize=12)
    ax.set_ylabel("Actual frequency", fontsize=12)
    ax.set_title(f"Calibration curve (n={n} resolved markets)", fontsize=13)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)

    out_path = Path(scores_csv).parent / f"calibration_{Path(scores_csv).stem}.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"Saved → {out_path}")
    plt.show()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python calibration_curve.py outputs/scores_results_<timestamp>.csv")
        sys.exit(1)
    main(sys.argv[1])
