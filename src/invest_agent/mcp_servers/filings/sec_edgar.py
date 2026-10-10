"""
mcp_servers/filings/sec_edgar.py: downloads a US company's facts file from SEC EDGAR.

What this file does:
    SEC publishes every figure a US company has reported in its filings as one JSON
    file per company, the "company facts" file, free and official (Tier 1). This file
    downloads it with the User-Agent header SEC requires (your name and email from
    .env). normaliser.py then turns it into six clean years per figure.

    The file can be several megabytes (Apple's is about 3.7 MB), so the timeout is
    longer than the usual 10 seconds, and the result is cached for 24 hours by the
    server.
"""

from __future__ import annotations

import httpx

from invest_agent.config import get_settings

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


class SecNotConfigured(RuntimeError):
    """SEC_EDGAR_USER_AGENT is missing from .env; SEC refuses requests without it."""


async def fetch_company_facts(cik: int | str) -> dict:
    agent = get_settings().sec_edgar_user_agent
    if not agent:
        raise SecNotConfigured("SEC_EDGAR_USER_AGENT is not set in .env")
    # {cik:010d} pads the company number to the 10 digits SEC's address needs.
    url = COMPANY_FACTS_URL.format(cik=int(cik))
    async with httpx.AsyncClient(headers={"User-Agent": agent}, follow_redirects=True, timeout=60) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()