"""
Smoke tests for the FX sources (build step 6): one real call each, through the same
code fx_server uses.

  Frankfurter   the primary source, free and keyless: today's USD to INR rate
  FRED          the cross-check, DEXINUS (skipped if FRED_API_KEY is not in .env)

Run with: uv run pytest tests/smoke/test_smoke_fx_server.py -v
"""

import asyncio
from datetime import date

import pytest

from invest_agent.config import get_settings
from invest_agent.fx.checks import business_days_between
from invest_agent.mcp_servers.fx import frankfurter, fred_fx


@pytest.mark.smoke
def test_frankfurter_usd_inr_rate():
    found = asyncio.run(frankfurter.latest_rate("USD", "INR"))
    # Sanity range, matching the verification layer's idea of a plausible rate.
    assert 50 < found["rate"] < 150
    assert business_days_between(found["date"], date.today()) <= 3       # not stale


@pytest.mark.smoke
def test_fred_dexinus():
    if not get_settings().fred_api_key:
        pytest.skip("FRED_API_KEY not set in .env")
    found = asyncio.run(fred_fx.latest_rate("USD", "INR"))
    assert 50 < found["rate"] < 150
