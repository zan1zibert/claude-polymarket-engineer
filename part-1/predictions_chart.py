"""
predictions_chart.py — per-market predictions coloured green/red by correctness,
grouped by confidence level.

Usage:
    python predictions_chart.py outputs/scores_results_<timestamp>.csv
"""
import sys
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import pandas as pd


def main(scores_csv: str):
    df = pd.read_csv(scores_csv)
    df = df[df["yes_won"].notna()].copy()
    df["yes_won"] = df["yes_won"].astype(float)

    if df.empty:
        print("No resolved markets in this file.")
        sys.exit(0)

    df["correct"] = (
        ((df["claude_prob"] >= 0.5) & (df["yes_won"] == 1.0)) |
        ((df["claude_prob"] <  0.5) & (df["yes_won"] == 0.0))
    )
    df["color"] = df["correct"].map({True: "#22c55e", False: "#ef4444"})
    df["label"] = df["question"].str[:55] + "…"

    confidence_levels = ["HIGH", "MEDIUM"]
    groups = {c: df[df["confidence"] == c].reset_index(drop=True)
              for c in confidence_levels if c in df["confidence"].values}

    fig, axes = plt.subplots(
        1, len(groups),
        figsize=(7 * len(groups), max(4, len(df) * 0.45)),
        sharey=False,
    )
    if len(groups) == 1:
        axes = [axes]

    for ax, (conf, grp) in zip(axes, groups.items()):
        grp = grp.sort_values("claude_prob").reset_index(drop=True)
        y = range(len(grp))

        # market price — grey diamond
        ax.scatter(grp["market_prob"], y, marker="D", color="#94a3b8",
                   s=60, zorder=3, label="Market price")

        # claude prediction — circle, green/red
        for i, row in grp.iterrows():
            ax.scatter(row["claude_prob"], i, marker="o",
                       color=row["color"], s=120, zorder=4,
                       edgecolors="white", linewidths=0.8)
            # line connecting market price to claude prediction
            ax.plot([row["market_prob"], row["claude_prob"]], [i, i],
                    color="#cbd5e1", linewidth=1, zorder=2)

        # 0.5 decision boundary
        ax.axvline(0.5, color="#64748b", linewidth=1, linestyle="--", alpha=0.5)

        ax.set_yticks(list(y))
        ax.set_yticklabels(grp["label"], fontsize=8)
        ax.set_xlim(0, 1)
        ax.set_xlabel("Probability", fontsize=10)
        ax.set_title(f"{conf} confidence  (n={len(grp)})", fontsize=12, fontweight="bold")
        ax.grid(True, alpha=0.2, axis="x")
        ax.invert_yaxis()

    # shared legend
    legend_handles = [
        mpatches.Patch(color="#22c55e", label="Correct"),
        mpatches.Patch(color="#ef4444", label="Incorrect"),
        plt.Line2D([0], [0], marker="D", color="w", markerfacecolor="#94a3b8",
                   markersize=8, label="Market price"),
    ]
    fig.legend(handles=legend_handles, loc="lower center",
               ncol=3, fontsize=10, frameon=True,
               bbox_to_anchor=(0.5, -0.02))

    fig.suptitle("Claude predictions: correct (green) vs incorrect (red)",
                 fontsize=13, fontweight="bold", y=1.01)

    plt.tight_layout()
    out_path = Path(scores_csv).parent / f"predictions_{Path(scores_csv).stem}.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved → {out_path}")
    plt.show()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python predictions_chart.py outputs/scores_results_<timestamp>.csv")
        sys.exit(1)
    main(sys.argv[1])
