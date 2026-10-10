"""
mcp_servers/macro_data/fred.py: US economic series from FRED, and the maths on them.

What this file does:
    FRED (the St. Louis Fed's data service) is free with a key and official (Tier 1).
    fetch() asks for one series' latest observations; the transform functions turn them
    into what the Macro agent reports: the latest level, a change over some months, or a
    year-on-year growth rate. Which series and which transform is set in series_map.yaml.

    Values arrive as text, with "." on days that have no figure (holidays); those are
    skipped. Every result keeps the date of the observation it came from.
"""

from __future__ import annotations

import httpx

from invest_agent.config import get_settings
from invest_agent.observability.tracked_call import tracked_call

URL = "https://api.stlouisfed.org/fred/series/observations"


class FredNotConfigured(RuntimeError):
    """FRED_API_KEY is missing from .env."""


def parse_observations(payload: dict) -> list[tuple[str, float]]:
    """[(date, value)], oldest first, without the "." placeholder days."""
    rows = [(o["date"], float(o["value"])) for o in payload.get("observations", [])
            if o.get("value") not in (None, "", ".")]
    return sorted(rows)


async def _get(series_id: str, frequency: str | None, limit: int) -> dict:
    params = {"series_id": series_id, "api_key": get_settings().fred_api_key, "file_type": "json",
              "sort_order": "desc", "limit": limit}
    if frequency:
        params.update({"frequency": frequency, "aggregation_method": "avg"})
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(URL, params=params)
        response.raise_for_status()
        return response.json()


async def fetch(series_id: str, frequency: str | None = None, limit: int = 30) -> list[tuple[str, float]]:
    if not get_settings().fred_api_key:
        raise FredNotConfigured("FRED_API_KEY is not set in .env")
    return parse_observations(await tracked_call("fred", series_id, _get, series_id, frequency, limit))


# --- Transforms (pure functions, tested offline) -----------------------------------------------

def level(rows: list[tuple[str, float]], months_back: int = 6) -> dict:
    """The latest value, and its change against the value months_back observations earlier
    (monthly series), in the series' own units (percentage points for rates)."""
    date, value = rows[-1]
    out = {"value": value, "date": date}
    if len(rows) > months_back:
        out[f"change_{months_back}m"] = round(value - rows[-1 - months_back][1], 4)
    return out


def yoy(rows: list[tuple[str, float]]) -> dict:
    """% change against 12 observations earlier (monthly index), e.g. CPI to inflation."""
    if len(rows) < 13:
        return {}
    date, value = rows[-1]
    out = {"value": round((value / rows[-13][1] - 1) * 100, 4), "date": date}
    if len(rows) >= 16:                          # the same three months earlier, for direction
        out["previous"] = round((rows[-4][1] / rows[-16][1] - 1) * 100, 4)
    return out


def yoy_quarter(rows: list[tuple[str, float]]) -> dict:
    """Quarterly series (real GDP): growth of the latest quarter on a year earlier, and
    the average annualised quarter-on-quarter growth of the last two quarters against
    the two before, which the growth rule compares."""
    if len(rows) < 6:
        return {}
    date, value = rows[-1]
    out: dict = {"date": date}
    if len(rows) >= 5:
        out["value"] = round((value / rows[-5][1] - 1) * 100, 4)
    qoq = [((rows[i][1] / rows[i - 1][1]) ** 4 - 1) * 100 for i in range(len(rows) - 4, len(rows))]
    out["recent_two_quarters"] = round(sum(qoq[2:]) / 2, 4)
    out["previous_two_quarters"] = round(sum(qoq[:2]) / 2, 4)
    return out


def change_12m(rows: list[tuple[str, float]]) -> dict:
    """% change over 12 months for a monthly series (a currency index or rate)."""
    if len(rows) < 13:
        return {}
    date, value = rows[-1]
    return {"value": value, "date": date, "change_12m_pct": round((value / rows[-13][1] - 1) * 100, 4)}


TRANSFORMS = {"level": level, "yoy": yoy, "yoy_quarter": yoy_quarter, "change_12m": change_12m}
