"""
mcp_servers/news/sec_8k.py: a US company's recent 8-K filings and what they are about.

What this file does:
    An 8-K is the form a US company files with SEC when something material happens. Each
    one lists item codes saying what it is about (design 14.6); the probe on 5 October
    showed SEC's submissions file gives them for every recent filing (Apple: 2.02 earnings
    in January, April and July; 5.02 executive changes; 5.07 a shareholder vote).

    This reads the 8-Ks from the last 90 days, names their items in plain words, and marks
    the serious ones: 1.03 bankruptcy and 4.02 a restatement cap the verdict at neutral
    whatever the score (design 14.11). The filings are official (Tier 1) facts, so they
    are not sent to the labelling model; plain code reads them.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from invest_agent.config import get_settings
from invest_agent.observability.tracked_call import tracked_call

URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DAYS = 90

ITEM_NAMES = {
    "1.01": "material agreement", "1.03": "bankruptcy or receivership", "1.05": "cybersecurity incident",
    "2.02": "earnings results", "2.06": "asset impairment", "3.01": "delisting notice",
    "4.01": "auditor change", "4.02": "past financial statements unreliable (restatement)",
    "5.02": "executive or director change", "5.07": "shareholder vote", "7.01": "Regulation FD disclosure",
    "8.01": "other event", "9.01": "financial statements and exhibits",
}
SERIOUS = {"1.03", "4.02"}
MATERIAL = {"1.01", "1.05", "2.06", "3.01", "4.01", "5.02"}


def parse_recent(payload: dict, today: date | None = None, days: int = DAYS) -> list[dict]:
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    recent = (payload.get("filings") or {}).get("recent") or {}
    out = []
    for form, filed, items, accession in zip(recent.get("form", []), recent.get("filingDate", []),
                                             recent.get("items", []), recent.get("accessionNumber", [])):
        if form != "8-K" or filed < cutoff:
            continue
        codes = [c.strip() for c in (items or "").split(",") if c.strip()]
        named = [f"{c} {ITEM_NAMES.get(c, 'other')}" for c in codes if c != "9.01"]
        out.append({"title": f"8-K filed {filed}: " + ("; ".join(named) or "exhibits only"),
                    "date": filed, "items": codes, "accession": accession, "origin": "sec_8k",
                    "serious": bool(SERIOUS & set(codes)), "material": bool(MATERIAL & set(codes)),
                    "url": None, "outlet": "SEC EDGAR", "trusted": True})
    return out


async def _get(cik: int) -> dict:
    async with httpx.AsyncClient(headers={"User-Agent": get_settings().sec_edgar_user_agent}, timeout=30) as client:
        response = await client.get(URL.format(cik=int(cik)))
        response.raise_for_status()
        return response.json()


async def recent_8ks(cik: int | str) -> list[dict]:
    return parse_recent(await tracked_call("sec_edgar", "submissions", _get, int(cik), timeout=30))
