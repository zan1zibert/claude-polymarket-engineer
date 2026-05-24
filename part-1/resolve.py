"""
resolve.py — fetch resolution outcomes for markets in a results CSV.

Usage:
    python resolve.py outputs/results_<timestamp>.csv
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

GAMMA_URL = "https://gamma-api.polymarket.com/markets/{}"


def fetch_outcome(market_id: str) -> dict:
    """Return resolution info for a single market id."""
    r = requests.get(GAMMA_URL.format(market_id), timeout=15)
    r.raise_for_status()
    d = r.json()

    closed = bool(d.get("closed"))
    outcomes = json.loads(d.get("outcomes") or "[]")
    prices = json.loads(d.get("outcomePrices") or "[]")

    yes_won = None
    if closed and outcomes and prices:
        try:
            price_map = {o.lower(): float(p) for o, p in zip(outcomes, prices)}
            yes_price = price_map.get("yes")
            if yes_price == 1.0:
                yes_won = True
            elif yes_price == 0.0:
                yes_won = False
        except (TypeError, ValueError):
            pass

    return {
        "id": str(market_id),
        "closed": closed,
        "yes_won": yes_won,   # True / False / None (not yet resolved)
    }


def main(results_csv: str):
    df = pd.read_csv(results_csv)
    if "id" not in df.columns:
        print("ERROR: CSV has no 'id' column")
        sys.exit(1)

    rows = []
    total = len(df)
    for i, market_id in enumerate(df["id"], start=1):
        print(f"[{i}/{total}] fetching {market_id}...")
        try:
            row = fetch_outcome(str(market_id))
        except Exception as e:
            print(f"  error: {e}")
            row = {"id": str(market_id), "closed": None, "yes_won": None}
        rows.append(row)
        if i < total:
            time.sleep(0.5)  # gentle on the API

    resolved_df = pd.DataFrame(rows)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(results_csv).parent / f"resolved_{timestamp}.csv"
    resolved_df.to_csv(out_path, index=False)

    resolved_count = resolved_df["yes_won"].notna().sum()
    print(f"\nResolved: {resolved_count}/{total} markets")
    print(f"Saved → {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python resolve.py outputs/results_<timestamp>.csv")
        sys.exit(1)
    main(sys.argv[1])
