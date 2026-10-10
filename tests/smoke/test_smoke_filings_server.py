"""
Smoke test: SEC EDGAR filings endpoint, the Tier 1 fundamentals source.

No API key needed, SEC requires a descriptive User-Agent header instead (set
SEC_EDGAR_USER_AGENT in .env, e.g. "Your Name your-email@example.com").

Run with: uv run pytest tests/smoke/test_smoke_filings_server.py -v
"""

import httpx
import pytest

from invest_agent.config import get_settings


@pytest.mark.smoke
def test_sec_edgar_company_lookup_reachable():
    settings = get_settings()
    assert settings.sec_edgar_user_agent, "Set SEC_EDGAR_USER_AGENT in .env before running this test"

    # company_tickers.json is SEC's own ticker-to-CIK mapping, a good lightweight reachability check.
    response = httpx.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": settings.sec_edgar_user_agent},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    assert len(data) > 1000  # sanity check: this file lists thousands of tickers
