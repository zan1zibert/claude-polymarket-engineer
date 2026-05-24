"""
edge_chart.py — per-market correctness, market vs Claude probability, sorted by edge.

Usage:
    python edge_chart.py outputs/scores_results_<timestamp>.csv
"""
import sys
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
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
    df = df.sort_values("edge").reset_index(drop=True)
    df["label"] = df["question"].str[:60] + "…"

    fig, ax = plt.subplots(figsize=(11, max(5, len(df) * 0.55)))

    for i, row in df.iterrows():
        # edge bar from market_prob to claude_prob
        bar_color = "#3b82f6" if row["edge"] >= 0 else "#f97316"
        ax.barh(i, row["edge"], left=row["market_prob"],
                height=0.4, color=bar_color, alpha=0.35, zorder=2)

        # market price — grey diamond
        ax.scatter(row["market_prob"], i, marker="D",
                   color="#64748b", s=70, zorder=4, label="Market" if i == 0 else "")

        # claude prediction — green/red circle
        dot_color = "#22c55e" if row["correct"] else "#ef4444"
        ax.scatter(row["claude_prob"], i, marker="o",
                   color=dot_color, s=110, zorder=5,
                   edgecolors="white", linewidths=0.8)

        # edge label on the right
        edge_str = f"{row['edge']:+.2f}"
        ax.text(1.02, i, edge_str, va="center", ha="left",
                fontsize=8, color=bar_color, fontweight="bold")

    ax.set_yticks(range(len(df)))
    ax.set_yticklabels(df["label"], fontsize=8)
    ax.set_xlim(0, 1)
    ax.set_xlabel("Probability", fontsize=10)
    ax.set_title("Claude vs market — sorted by edge (Claude − market)",
                 fontsize=12, fontweight="bold")
    ax.axvline(0.5, color="#94a3b8", linewidth=1, linestyle="--", alpha=0.6)
    ax.grid(True, alpha=0.2, axis="x")
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(xmax=1))

    legend_handles = [
        mpatches.Patch(color="#22c55e", label="Claude correct"),
        mpatches.Patch(color="#ef4444", label="Claude incorrect"),
        plt.Line2D([0], [0], marker="D", color="w",
                   markerfacecolor="#64748b", markersize=8, label="Market price"),
        mpatches.Patch(color="#3b82f6", alpha=0.5, label="Positive edge (Claude > market)"),
        mpatches.Patch(color="#f97316", alpha=0.5, label="Negative edge (Claude < market)"),
    ]
    ax.legend(handles=legend_handles, fontsize=8, loc="lower right")

    plt.tight_layout()
    out_path = Path(scores_csv).parent / f"edge_{Path(scores_csv).stem}.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved → {out_path}")
    plt.show()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python edge_chart.py outputs/scores_results_<timestamp>.csv")
        sys.exit(1)
    main(sys.argv[1])
