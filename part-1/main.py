import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from rich.console import Console

from utils.claude import analyze_market
from utils.polymarket import get_active_markets

console = Console()

OUTPUTS_DIR = Path(__file__).resolve().parent / "outputs"
SAMPLE_SIZE = 40
PAUSE_BETWEEN_MARKETS = 30  # seconds — stays well under 30k TPM rate limit


console.print("[bold green]Claude vs Polymarket - Part 1[/bold green]")
console.print("Fetching active markets...\n")

OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
snapshot_path = OUTPUTS_DIR / f"markets_{timestamp}.json"
results_jsonl_path = OUTPUTS_DIR / f"results_{timestamp}.jsonl"
results_csv_path = OUTPUTS_DIR / f"results_{timestamp}.csv"

markets = get_active_markets(
    limit=150,
    hours_until_resolution=168,
    save_to=str(snapshot_path),
)
console.print(f"Got {len(markets)} markets matching filters\n")

sample = markets[:SAMPLE_SIZE]
results = []

with open(results_jsonl_path, "w") as jsonl_file:
    for i, market in enumerate(sample, start=1):
        console.print(f"[{i}/{len(sample)}] Analyzing: {market['question'][:80]}...")

        analysis = analyze_market(market)

        if "error" in analysis:
            console.print("  [yellow]parse error, skipped[/yellow]")
        else:
            confidence = analysis.get("confidence", "MEDIUM")
            if confidence != "LOW":
                row = {
                    "id": market["id"],
                    "question": market["question"][:120],
                    "market_prob": market["yes_price"],
                    "claude_prob": round(float(analysis.get("probability", 0)), 4),
                    "confidence": confidence,
                    "web_searches": analysis.get("web_searches", 0),
                    "reasoning": analysis.get("reasoning", ""),
                    "end_date": market["end_date"],
                }
                results.append(row)
                jsonl_file.write(json.dumps(row) + "\n")
                jsonl_file.flush()
                console.print(f"  [green]saved[/green] ({len(results)} so far)")
            else:
                console.print("  [yellow]low confidence, skipped[/yellow]")

        if i < len(sample):
            console.print(f"  [dim]waiting {PAUSE_BETWEEN_MARKETS}s...[/dim]")
            time.sleep(PAUSE_BETWEEN_MARKETS)

df = pd.DataFrame(results)
df["edge"] = (df["claude_prob"] - df["market_prob"]).round(4)

console.print("\n[bold]Results:[/bold]")
console.print(
    df[["question", "market_prob", "claude_prob", "edge", "confidence", "web_searches"]]
    .to_string(index=False)
)

df.to_csv(results_csv_path, index=False)
console.print(f"\nResults (CSV)   → {results_csv_path}")
console.print(f"Results (JSONL) → {results_jsonl_path}")
console.print(f"Market snapshot → {snapshot_path}")
