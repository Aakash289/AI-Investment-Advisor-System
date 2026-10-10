"""
mcp_servers/macro_data/worldbank.py: the World Bank's annual figures, the fallback for the panel.

What this file does:
    The World Bank's API is free with no key. Its figures are actual annual outcomes,
    published a year or so after the year ends, so they are used only when the IMF call
    fails: GDP growth (NY.GDP.MKTP.KD.ZG) and inflation (FP.CPI.TOTL.ZG), latest two
    years per country. Values used from here are flagged as lagged.
"""

from __future__ import annotations

import httpx

from invest_agent.observability.tracked_call import tracked_call

URL = "https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"


def parse(payload: list) -> list[tuple[int, float]]:
    """[(year, value)], oldest first. The answer is [paging info, rows]."""
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and payload[1] else []
    return sorted((int(r["date"]), float(r["value"])) for r in rows if r.get("value") is not None)


async def _get(country: str, indicator: str) -> list:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(URL.format(country=country, indicator=indicator), params={"format": "json", "mrv": 3})
        response.raise_for_status()
        return response.json()


async def latest(country: str, indicator: str) -> dict:
    rows = parse(await tracked_call("worldbank", indicator, _get, country, indicator, timeout=20))
    if not rows:
        return {}
    out = {"value": rows[-1][1], "date": str(rows[-1][0]), "lagged": True}
    if len(rows) > 1:
        out["previous"] = rows[-2][1]
    return out
