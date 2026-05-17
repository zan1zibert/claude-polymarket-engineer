from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rich.console import Console

from utils.claude import analyze_market
from utils.polymarket import get_active_markets

console = Console()

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"
SAMPLE_SIZE = 30


def main():
    console.print("[bold green]Claude vs Polymarket - Part 1[/bold green]")
    console.print("Fetching active markets...\n")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_path = OUTPUTS_DIR / f"markets_{timestamp}.json"

    markets = get_active_markets(
        limit=500,
        hours_until_resolution=24,
        save_to=str(snapshot_path),
    )
    console.print(f"Got {len(markets)} markets matching filters\n")

    sample = markets[:SAMPLE_SIZE]
    results = []

    for i, market in enumerate(sample, start=1):
        console.print(f"[{i}/{len(sample)}] Analyzing: {market['question'][:80]}...")

        analysis = analyze_market(market)

        if "error" in analysis:
            console.print("  [yellow]parse error, skipped[/yellow]")
            continue

        results.append({
            "id": market["id"],
            "question": market["question"][:120],
            "market_prob": market["yes_price"],
            "claude_prob": round(float(analysis.get("probability", 0)), 4),
            "confidence": analysis.get("confidence", "MEDIUM"),
            "end_date": market["end_date"],
        })

    df = pd.DataFrame(results)
    df["edge"] = (df["claude_prob"] - df["market_prob"]).round(4)

    console.print("\n[bold]Results:[/bold]")
    console.print(
        df[["question", "market_prob", "claude_prob", "edge", "confidence"]]
        .to_string(index=False)
    )

    results_path = OUTPUTS_DIR / f"results_{timestamp}.csv"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(results_path, index=False)
    console.print(f"\nResults saved to {results_path}")
    console.print(f"Market snapshot saved to {snapshot_path}")
