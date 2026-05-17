import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def fetch_markets(limit: int, end_date_max: str) -> List[Dict]:
    """Raw Gamma API call. Returns markets ordered by 24h volume desc."""
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
        "end_date_max": end_date_max,
    }
    response = requests.get(GAMMA_MARKETS_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _parse_json_field(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _normalize(market: Dict) -> Optional[Dict]:
    """Flatten a Gamma payload into the fields we use downstream. Drops non-binary markets."""
    outcomes = _parse_json_field(market.get("outcomes"), [])
    prices = _parse_json_field(market.get("outcomePrices"), [])

    if sorted(o.lower() for o in outcomes) != ["no", "yes"]:
        return None
    if len(prices) != 2:
        return None

    try:
        price_map = {o.lower(): float(p) for o, p in zip(outcomes, prices)}
    except (TypeError, ValueError):
        return None

    return {
        "id": market.get("id"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "description": market.get("description", ""),
        "end_date": market.get("endDate"),
        "yes_price": round(price_map["yes"], 4),
        "volume": float(market.get("volumeNum") or market.get("volume") or 0),
        "volume_24h": float(market.get("volume24hr") or 0),
        "liquidity": float(market.get("liquidityNum") or market.get("liquidity") or 0),
    }


def filter_markets(
    markets: List[Dict],
    min_volume_24h: float = 5_000,
    min_liquidity: float = 10_000,
    price_band: tuple = (0.15, 0.85),
) -> List[Dict]:
    """Keep liquid binary markets in the interesting price band — see Part 1 README."""
    lo, hi = price_band
    return [
        m for m in markets
        if m["volume_24h"] >= min_volume_24h
        and m["liquidity"] >= min_liquidity
        and lo <= m["yes_price"] <= hi
    ]


def get_active_markets(
    limit: int = 50,
    hours_until_resolution: int = 24,
    min_volume_24h: float = 5_000,
    min_liquidity: float = 10_000,
    price_band: tuple = (0.15, 0.95),
    save_to: Optional[str] = None,
) -> List[Dict]:
    """Fetch, normalize, filter, and optionally persist a market snapshot to JSON."""
    end_date_max = (
        datetime.now(timezone.utc) + timedelta(hours=hours_until_resolution)
    ).isoformat()

    raw = fetch_markets(limit=limit, end_date_max=end_date_max)
    normalized = [n for n in (_normalize(m) for m in raw) if n is not None]
    filtered = filter_markets(
        normalized,
        min_volume_24h=min_volume_24h,
        min_liquidity=min_liquidity,
        price_band=price_band,
    )

    if save_to:
        path = Path(save_to)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "filters": {
                "hours_until_resolution": hours_until_resolution,
                "min_volume_24h": min_volume_24h,
                "min_liquidity": min_liquidity,
                "price_band": list(price_band),
            },
            "count": len(filtered),
            "markets": filtered,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

    return filtered


if __name__ == "__main__":
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = (
        Path(__file__).resolve().parent.parent / "outputs" / f"markets_{timestamp}.json"
    )
    markets = get_active_markets(save_to=str(out_path))
    print(f"Saved {len(markets)} markets to {out_path}")
