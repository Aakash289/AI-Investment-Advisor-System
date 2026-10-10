"""
mcp_servers/macro_data/bis.py: central bank policy rates from the BIS (design 14.5).

What this file does:
    The Bank for International Settlements publishes every major central bank's policy
    rate as free data with no key (dataset WS_CBPOL). It replaces the RBI as a direct
    source, since the RBI has no official API. The probe on 5 October confirmed India is
    included (the repo rate, 5.25%) and that the daily series answers as CSV.

    One call fetches the panel's countries at once (IN+CN+XM+JP+GB). The BIS runs about
    two to three months behind, so every value keeps its date and the agent flags it as
    stale only past the limit in regime_rules.yaml.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import httpx

from invest_agent.observability.tracked_call import tracked_call

URL = "https://stats.bis.org/api/v1/data/WS_CBPOL/D.{codes}/all"


def parse_csv(text: str) -> dict[str, list[tuple[str, float]]]:
    """{BIS country code: [(date, rate)], oldest first} from the CSV answer."""
    out: dict[str, list[tuple[str, float]]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        value = row.get("OBS_VALUE")
        if row.get("REF_AREA") and row.get("TIME_PERIOD") and value not in (None, "", "NaN"):
            out.setdefault(row["REF_AREA"], []).append((row["TIME_PERIOD"], float(value)))
    return {code: sorted(rows) for code, rows in out.items()}


async def _get(codes: list[str], last_n: int) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(URL.format(codes="+".join(codes)),
                                    params={"lastNObservations": last_n, "format": "csv"})
        response.raise_for_status()
        return response.text


async def policy_rates(codes: list[str], last_n: int = 200) -> dict[str, list[tuple[str, float]]]:
    """About 200 business days per country: enough to measure the six-month change."""
    return parse_csv(await tracked_call("bis", "policy_rates", _get, codes, last_n, timeout=30))


def latest_and_change(rows: list[tuple[str, float]], months: int = 6) -> dict:
    """The latest rate, and its change against the rate in force `months` earlier."""
    latest_date, latest = rows[-1]
    out = {"value": latest, "date": latest_date}
    cutoff = (date.fromisoformat(latest_date) - timedelta(days=round(months * 30.44))).isoformat()
    earlier = [v for d, v in rows if d <= cutoff]
    if earlier:
        out[f"change_{months}m"] = round(latest - earlier[-1], 4)
    return out
