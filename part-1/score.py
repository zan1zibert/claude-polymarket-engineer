"""
score.py — compute Brier score and calibration stats from results + resolved CSVs.

Usage:
    python score.py outputs/results_<timestamp>.csv outputs/resolved_<timestamp>.csv
"""
import sys
from pathlib import Path

import pandas as pd


def brier(probs, outcomes):
    return ((probs - outcomes) ** 2).mean()


def main(results_csv: str, resolved_csv: str):
    results = pd.read_csv(results_csv)
    resolved = pd.read_csv(resolved_csv)

    # join and drop unresolved markets
    df = results.merge(resolved[["id", "yes_won"]], on="id", how="inner")
    df = df[df["yes_won"].notna()].copy()
    df["yes_won"] = df["yes_won"].astype(float)

    if df.empty:
        print("No resolved markets yet — come back later.")
        return

    n = len(df)
    print(f"\n{'='*50}")
    print(f"  Resolved markets: {n}")
    print(f"{'='*50}\n")

    # --- overall Brier scores ---
    bs_claude = brier(df["claude_prob"], df["yes_won"])
    bs_market = brier(df["market_prob"], df["yes_won"])
    bs_naive  = brier(pd.Series([0.5] * n), df["yes_won"])

    print("Brier scores (lower is better, 0.25 = coin flip):")
    print(f"  Market price : {bs_market:.4f}")
    print(f"  Claude       : {bs_claude:.4f}")
    print(f"  Naive 50%    : {bs_naive:.4f}")
    delta = bs_claude - bs_market
    sign = "+" if delta > 0 else ""
    print(f"  Claude vs market: {sign}{delta:.4f}  ({'worse' if delta > 0 else 'better'})\n")

    # --- by confidence bucket ---
    print("Brier by confidence bucket:")
    for conf, grp in df.groupby("confidence"):
        bs = brier(grp["claude_prob"], grp["yes_won"])
        print(f"  {conf:<8} n={len(grp):>3}   Brier={bs:.4f}")
    print()

    # --- calibration table ---
    print("Calibration (predicted vs actual frequency):")
    print(f"  {'Predicted band':<18} {'n':>4}  {'Avg predicted':>14}  {'Actual frequency':>16}")
    bins = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    labels = ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"]
    df["band"] = pd.cut(df["claude_prob"], bins=bins, labels=labels, include_lowest=True)
    for band, grp in df.groupby("band", observed=True):
        avg_pred = grp["claude_prob"].mean()
        actual_freq = grp["yes_won"].mean()
        print(f"  {str(band):<18} {len(grp):>4}  {avg_pred:>13.1%}  {actual_freq:>15.1%}")
    print()

    # --- save aggregate CSV ---
    out_path = Path(results_csv).parent / f"scores_{Path(results_csv).stem}.csv"
    summary = df[["id", "question", "market_prob", "claude_prob", "confidence",
                  "yes_won", "edge"]].copy()
    summary["brier_claude"] = round((summary["claude_prob"] - summary["yes_won"]) ** 2, 4)
    summary["brier_market"] = round((summary["market_prob"] - summary["yes_won"]) ** 2, 4)
    summary.to_csv(out_path, index=False)
    print(f"Per-market scores saved → {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python score.py outputs/results_<timestamp>.csv outputs/resolved_<timestamp>.csv")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
