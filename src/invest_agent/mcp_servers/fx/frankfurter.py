"""
mcp_servers/fx/frankfurter.py: exchange rates from Frankfurter, the primary FX source.

What this file does:
    Frankfurter (api.frankfurter.dev) is free, needs no key and has no daily or monthly
    quota. It passes through official reference rates from central banks and official
    bodies, including FBIL (Financial Benchmarks India) for the rupee (design 14.8).
    This file makes three kinds of call, all on version 2 of its API:

      latest_rate(base, quote, provider)   /v2/rate/USD/INR, optionally pinned to one
                                           provider such as FBIL
      rate_on(base, quote, day)            the same for a past date (used for the FRED
                                           cross-check, which is always a week or so behind)
      series(base, quote, start, end)      one rate a month between two dates (used for
                                           the 12-month currency risk note)

    A rate always means "units of quote for one unit of base": USD to INR 96.2 is
    96.2 rupees per dollar.

    What the probe found on 5 October 2026 (scripts/fx_probe.py):
      the default rate is a blend of official sources and is dated today, even on
      a Sunday; the FBIL-only rate was dated 28 September, because FBIL's feed into
      Frankfurter had missed four publications. So the server tries FBIL first and
      falls back to the blend when FBIL is more than three business days old.

Every call goes through tracked_call, so it is timed, retried on network trouble and
logged like any other outside call.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# httpx is the HTTP library used across the project; AsyncClient makes requests
# without blocking the other agents running at the same time.
import httpx

from invest_agent.observability.tracked_call import tracked_call

BASE_URL = "https://api.frankfurter.dev"


class FxDataError(RuntimeError):
    """Frankfurter answered, but not with a usable rate."""


async def _get_json(path: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as client:
        response = await client.get(path, params=params)
        response.raise_for_status()          # 4xx and 5xx become errors tracked_call can judge
        return response.json()


def parse_rate(data: Any, base: str, quote: str) -> dict:
    """{"rate", "date"} from a /v2/rate answer, e.g. {"date": "2026-10-05", "base": "USD",
    "quote": "INR", "rate": 96.2}. The older /v1 shape ({"rates": {"INR": 96.2}}) is
    also accepted, in case the API ever answers that way."""
    if isinstance(data, dict):
        rate = data.get("rate")
        if rate is None and isinstance(data.get("rates"), dict):
            rate = data["rates"].get(quote)
        if rate is not None and data.get("date"):
            return {"rate": float(rate), "date": str(data["date"])[:10]}
    raise FxDataError(f"Frankfurter gave no {base}/{quote} rate: {str(data)[:200]}")


def parse_series(data: Any) -> list[dict]:
    """[{"date", "rate"}], oldest first, from a /v2/rates answer (a list of rows)."""
    rows = data if isinstance(data, list) else (data or {}).get("rates", [])
    points = [{"date": str(r["date"])[:10], "rate": float(r["rate"])}
              for r in rows if isinstance(r, dict) and r.get("rate") is not None and r.get("date")]
    if not points:
        raise FxDataError("Frankfurter gave an empty series")
    return sorted(points, key=lambda p: p["date"])


async def latest_rate(base: str, quote: str, provider: str | None = None) -> dict:
    """The latest rate; with provider (e.g. "FBIL") only that provider's rate."""
    params = {"providers": provider} if provider else None
    data = await tracked_call("frankfurter", "rate", _get_json, f"/v2/rate/{base}/{quote}", params)
    return parse_rate(data, base, quote)


async def rate_on(base: str, quote: str, day: str | date) -> dict:
    """The rate for a given date (Frankfurter gives the nearest earlier one if needed)."""
    data = await tracked_call("frankfurter", "rate_on", _get_json, f"/v2/rate/{base}/{quote}",
                              {"date": str(day)[:10]})
    return parse_rate(data, base, quote)


async def series(base: str, quote: str, start: date, end: date, group: str = "month") -> list[dict]:
    """One rate per month (group="month") between start and end."""
    data = await tracked_call("frankfurter", "series", _get_json, "/v2/rates",
                              {"base": base, "quotes": quote, "from": start.isoformat(),
                               "to": end.isoformat(), "group": group})
    return parse_series(data)
