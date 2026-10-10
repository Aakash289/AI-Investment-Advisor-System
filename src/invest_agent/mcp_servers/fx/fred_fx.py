"""
mcp_servers/fx/fred_fx.py: the US Federal Reserve's dollar to rupee rate, from FRED.

What this file does:
    FRED (the St. Louis Fed's data service) publishes DEXINUS: rupees per US dollar, an
    official daily series. It runs about a week behind (the probe on 5 October 2026 saw
    its latest value dated 25 September), so it is not the main source. It is used two
    ways (design 14.8):

      cross-check   its latest value is compared with Frankfurter's rate for the same
                    date; a gap of more than 1% is flagged
      fallback      if Frankfurter is down, its latest value is used, flagged as lagged

    Only the dollar and rupee pair is covered. FRED needs FRED_API_KEY in .env; without
    it this source simply reports itself unavailable and the server carries on.

    The key travels as a request parameter. If a request ever fails, the error text can
    contain the full web address with the key in it, which is why every log line is
    cleaned by the rule in invest_agent/__init__.py.
"""

from __future__ import annotations

import httpx

from invest_agent.config import get_settings
from invest_agent.observability.tracked_call import tracked_call

URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES_ID = "DEXINUS"          # Indian rupees to one US dollar


class FxSourceUnavailable(RuntimeError):
    """This source cannot be used for this pair (no key, or a pair it does not cover)."""


def supports(base: str, quote: str) -> bool:
    return {base, quote} == {"USD", "INR"}


def parse_latest(observations: list[dict]) -> dict:
    """The newest real value. FRED writes "." for days with no rate (holidays)."""
    for obs in observations:                       # asked for newest first
        value = obs.get("value")
        if value not in (None, "", "."):
            return {"rate": float(value), "date": str(obs["date"])[:10]}
    raise FxSourceUnavailable("FRED returned no DEXINUS values")


async def _fetch(limit: int) -> list[dict]:
    params = {"series_id": SERIES_ID, "api_key": get_settings().fred_api_key, "file_type": "json",
              "sort_order": "desc", "limit": limit}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(URL, params=params)
        response.raise_for_status()
        return response.json().get("observations", [])


async def latest_rate(base: str, quote: str) -> dict:
    """{"rate", "date"} as quote per base: 95.81 for USD to INR, 1/95.81 for INR to USD."""
    if not supports(base, quote):
        raise FxSourceUnavailable(f"FRED has no {base}/{quote} series here")
    if not get_settings().fred_api_key:
        raise FxSourceUnavailable("FRED_API_KEY is not set")
    observations = await tracked_call("fred", "dexinus", _fetch, 10)
    latest = parse_latest(observations)
    if base == "INR":                               # DEXINUS is rupees per dollar
        latest["rate"] = 1 / latest["rate"]
    return latest
