"""
mcp_servers/macro_data/imf.py: growth and inflation for the panel from the IMF (design 14.5).

What this file does:
    The IMF's DataMapper API is free with no key and serves the World Economic Outlook
    figures: one value a year per country, including the IMF's estimate for the current
    year and its forecasts. The Macro agent uses real GDP growth (NGDP_RPCH) and
    inflation (PCPIPCH) for every panel country except the US, whose monthly and
    quarterly figures come from FRED.

    The probe on 5 October found the API ignores the country list in the address and
    returns every country, so one call per indicator serves the whole panel and the
    code picks out the countries it needs.

    For India the IMF's years are fiscal years (April to March).
"""

from __future__ import annotations

import httpx

from invest_agent.observability.tracked_call import tracked_call

URL = "https://www.imf.org/external/datamapper/api/v1/{indicator}"


def parse(payload: dict, indicator: str) -> dict[str, dict[int, float]]:
    """{IMF country code: {year: value}}."""
    values = (payload.get("values") or {}).get(indicator) or {}
    return {country: {int(y): float(v) for y, v in years.items() if v is not None}
            for country, years in values.items()}


async def _get(indicator: str) -> dict:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(URL.format(indicator=indicator))
        response.raise_for_status()
        return response.json()


async def indicator(code: str) -> dict[str, dict[int, float]]:
    return parse(await tracked_call("imf", code, _get, code, timeout=30), code)


def this_and_last_year(years: dict[int, float], current_year: int) -> dict:
    """The IMF's figure for the current year (an estimate) and the year before."""
    out: dict = {}
    if current_year in years:
        out.update({"value": years[current_year], "date": str(current_year), "estimate": True})
    if current_year - 1 in years:
        out["previous"] = years[current_year - 1]
    return out
