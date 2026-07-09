"""Polymarket Gamma API client — the one place that knows the market shape.

The market-syncer uses this to (a) discover fresh binary markets resolving soon
and (b) check whether markets we already store have resolved. Logic is ported
from part-1's `utils/polymarket.py`, but uses `httpx` (already a shared dep) and
is split so the syncer can fetch candidates and statuses separately.

A "binary" market is one with exactly Yes/No outcomes; we drop everything else
because the worker scores a single 0..1 probability.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# Gamma rejects very long query strings; cap how many ids we ask about per call.
_STATUS_CHUNK = 100


def _parse_json_field(value, default):
    """Gamma sometimes returns list fields as JSON-encoded strings."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def resolved_yes_price(market: dict) -> Optional[float]:
    """Settled YES outcome for a resolved binary market: 1.0 if YES won, 0.0 if NO
    won, or None if it isn't definitively settled. Gamma reports outcomePrices as
    ["1","0"]/["0","1"] once resolved; we only trust a value within 1e-3 of 0 or 1,
    so a market that's closed but not yet settled (or non-binary) yields None."""
    outcomes = _parse_json_field(market.get("outcomes"), [])
    prices = _parse_json_field(market.get("outcomePrices"), [])
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    try:
        price_map = {o.lower(): float(p) for o, p in zip(outcomes, prices)}
    except (TypeError, ValueError):
        return None
    yes = price_map.get("yes")
    if yes is None:
        return None
    if abs(yes) <= 1e-3 or abs(yes - 1.0) <= 1e-3:
        return float(round(yes))
    return None


def normalize(market: dict) -> Optional[dict]:
    """Flatten a Gamma payload into the fields we store. None if non-binary/unusable."""
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
        "id": str(market.get("id")),
        "slug": market.get("slug"),
        "question": market.get("question") or "",
        "description": market.get("description") or "",
        "end_date": market.get("endDate"),
        "yes_price": round(price_map["yes"], 4),
        "volume_24h": float(market.get("volume24hr") or 0),
        "liquidity": float(market.get("liquidityNum") or market.get("liquidity") or 0),
    }


def fetch_markets(
    client: httpx.Client,
    window_days: int,
    limit: int,
    tag_id: int,
    url: str = GAMMA_MARKETS_URL
) -> list[dict]:
    """Active, open binary markets resolving within `window_days`, normalized.

    Ordered by 24h volume desc so the `limit` we take is the most liquid slice.
    """
    now = datetime.now(timezone.utc)
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "tag_id": tag_id,
        "order": "volume24hr",
        "ascending": "false",
        "end_date_min": now.isoformat(),
        "end_date_max": (now + timedelta(days=window_days)).isoformat(),
    }
    resp = client.get(url, params=params)
    resp.raise_for_status()
    return [n for m in resp.json() if (n := normalize(m)) is not None]


def filter_markets(
    markets: list[dict],
    min_volume_24h: float,
    min_liquidity: float,
    price_band: tuple[float, float],
) -> list[dict]:
    """Keep liquid binary markets in the interesting price band (see Part 1)."""
    lo, hi = price_band
    return [
        m for m in markets
        if m["volume_24h"] >= min_volume_24h
        and m["liquidity"] >= min_liquidity
        and lo <= m["yes_price"] <= hi
    ]


def fetch_statuses(
    client: httpx.Client,
    ids: list[str],
    url: str = GAMMA_MARKETS_URL
) -> dict[str, dict]:
    """Current {closed, end_date, resolved_outcome} per id we still store, for the resolve step.

    Queried in chunks to keep the URL short. Ids Gamma no longer returns are
    simply absent from the result; the caller treats "missing" as resolved.
    """
    statuses: dict[str, dict] = {}
    for start in range(0, len(ids), _STATUS_CHUNK):
        chunk = ids[start:start + _STATUS_CHUNK]
        # Repeated `id` params: ?id=1&id=2&...
        params = [("id", i) for i in chunk]
        params.append(("limit", len(chunk)))
        resp = client.get(url, params=params)
        resp.raise_for_status()
        for m in resp.json():
            statuses[str(m.get("id"))] = {
                "closed": bool(m.get("closed")),
                "end_date": m.get("endDate"),
                "resolved_outcome": resolved_yes_price(m),
            }
    return statuses
