"""
mcp_servers/market_data/massive.py: the US price fallback, Massive's free plan.

What this file does:
    When Yahoo fails for a US stock, this fetches the same two years of daily prices
    from Massive (formerly Polygon.io) and returns them in the same row shape as
    yahoo.py, so nothing downstream needs to know which source answered.

    Free plan: end-of-day prices, two years of history, 5 calls a minute (tracked_call
    spaces calls 12 seconds apart, set in observability/pricing.yaml). It covers US
    stocks only; Indian stocks fall back to Trendlyne instead (build step 5).

    Massive returns split-adjusted prices only, so close and adj_close are the same
    here. The last day's adjusted close equals the actual last price, so the current
    price is still correct.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from invest_agent.config import get_settings

# api.polygon.io still works during the rename to Massive (design 14.4).
AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"


class MassiveNotConfigured(RuntimeError):
    """No POLYGON_API_KEY in .env, so the fallback cannot be used."""


async def fetch_history(symbol: str, years: int = 2) -> list[dict]:
    """Daily rows {date, close, adj_close, volume}, oldest first."""
    key = get_settings().polygon_api_key
    if not key:
        raise MassiveNotConfigured("POLYGON_API_KEY is not set in .env")

    end = date.today()
    start = end - timedelta(days=365 * years)
    url = AGGS_URL.format(symbol=symbol.upper(), start=start.isoformat(), end=end.isoformat())
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    rows = []
    for bar in data.get("results") or []:
        # Massive's short field names: t = time in milliseconds, c = close, v = volume.
        day = date.fromtimestamp(bar["t"] / 1000).isoformat()
        rows.append({"date": day, "close": float(bar["c"]), "adj_close": float(bar["c"]),
                     "volume": float(bar.get("v") or 0)})
    if not rows:
        raise RuntimeError(f"Massive returned no prices for {symbol}")
    return rows