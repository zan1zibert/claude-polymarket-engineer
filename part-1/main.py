from utils.polymarket import get_active_markets
from utils.claude import analyze_market
from rich.console import Console
from rich.table import Table
import pandas as pd

console = Console()

def main():
    console.print("[bold green]Claude vs Polymarket - Part 1[/bold green]")
    console.print("Fetching active markets...\n")
    
    markets = get_active_markets(limit=100)
    
    results = []
    
    for i, market in enumerate(markets[:8]):  # Analyze first 8 for speed
        console.print(f"[{i+1}/8] Analyzing: {market['question'][:80]}...")
        
        analysis = analyze_market(market)
        
        if "error" not in analysis:
            results.append({
                "question": market['question'][:80],
                "market_prob": market['yes_price'],
                "claude_prob": round(analysis.get('probability', 0) * 100, 1),
                "confidence": analysis.get('confidence', 'MEDIUM')
            })
    
    # Display results
    df = pd.DataFrame(results)
    console.print("\n[bold]Results:[/bold]")
    console.print(df.to_string(index=False))
    
    # Save
    df.to_csv("part-1/outputs/results_part1.csv", index=False)
    console.print("\n✅ Results saved to part-1/outputs/results_part1.csv")

if __name__ == "__main__":
    main()