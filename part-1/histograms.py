"""
histograms.py — distribution plots segmented by HIGH vs MEDIUM confidence.

Usage:
    python histograms.py outputs/scores_results_<timestamp>.csv
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

COLORS = {"HIGH": "#2563eb", "MEDIUM": "#f59e0b"}
BINS = 10


def plot_hist(ax, df, col, title, xlabel, bins=BINS):
    for conf, grp in df.groupby("confidence"):
        ax.hist(
            grp[col],
            bins=bins,
            alpha=0.6,
            color=COLORS.get(conf, "grey"),
            label=f"{conf} (n={len(grp)})",
            edgecolor="white",
            linewidth=0.5,
        )
    ax.set_title(title, fontsize=12)
    ax.set_xlabel(xlabel, fontsize=10)
    ax.set_ylabel("Count", fontsize=10)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")


def main(scores_csv: str):
    df = pd.read_csv(scores_csv)
    df = df[df["yes_won"].notna()].copy()

    if df.empty:
        print("No resolved markets in this file.")
        sys.exit(0)

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle(
        f"Prediction distributions by confidence  (n={len(df)} resolved markets)",
        fontsize=14,
        fontweight="bold",
    )

    plot_hist(axes[0, 0], df, "claude_prob",
              "Claude predicted probability", "Probability")

    plot_hist(axes[0, 1], df, "brier_claude",
              "Per-market Brier score (Claude)", "Brier score")

    plot_hist(axes[1, 0], df, "edge",
              "Edge  (Claude − market)", "Claude prob − market prob")

    # outcome breakdown bar chart: correct vs incorrect by confidence
    ax = axes[1, 1]
    df["correct"] = (
        ((df["claude_prob"] >= 0.5) & (df["yes_won"] == 1.0)) |
        ((df["claude_prob"] <  0.5) & (df["yes_won"] == 0.0))
    )
    summary = df.groupby("confidence")["correct"].agg(["sum", "count"])
    summary["wrong"] = summary["count"] - summary["sum"]
    summary = summary.rename(columns={"sum": "correct"})

    x = range(len(summary))
    width = 0.35
    ax.bar([i - width / 2 for i in x], summary["correct"],
           width, label="Correct", color="#22c55e", edgecolor="white")
    ax.bar([i + width / 2 for i in x], summary["wrong"],
           width, label="Wrong", color="#ef4444", edgecolor="white")
    ax.set_xticks(list(x))
    ax.set_xticklabels(summary.index, fontsize=11)
    ax.set_title("Directional accuracy by confidence", fontsize=12)
    ax.set_ylabel("Count", fontsize=10)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    out_path = Path(scores_csv).parent / f"histograms_{Path(scores_csv).stem}.png"
    plt.savefig(out_path, dpi=150)
    print(f"Saved → {out_path}")
    plt.show()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python histograms.py outputs/scores_results_<timestamp>.csv")
        sys.exit(1)
    main(sys.argv[1])
