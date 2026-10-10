"""
Smoke test: FRED, the Tier 1 macro data source. Needs FRED_API_KEY in .env
(free key at https://fred.stlouisfed.org/docs/api/api_key.html).

Run with: uv run pytest tests/smoke/test_smoke_macro_data_server.py -v
"""

import httpx
import pytest

from invest_agent.config import get_settings


@pytest.mark.smoke
def test_fred_series_lookup():
    settings = get_settings()
    if not settings.fred_api_key:
        pytest.skip("FRED_API_KEY not set in .env")

    # DFF = effective federal funds rate, a small, stable series, good for a reachability check.
    response = httpx.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": "DFF",
            "api_key": settings.fred_api_key,
            "file_type": "json",
            "limit": 1,
            "sort_order": "desc",
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    assert "observations" in data
    assert len(data["observations"]) == 1


@pytest.mark.smoke
def test_bis_has_india_policy_rate():
    """BIS policy rates (no key): India's repo rate, through the same code the server uses."""
    import asyncio

    from invest_agent.mcp_servers.macro_data import bis
    rates = asyncio.run(bis.policy_rates(["IN"], last_n=5))
    assert rates.get("IN") and 0 < rates["IN"][-1][1] < 20


@pytest.mark.smoke
def test_imf_growth_for_the_panel():
    """IMF DataMapper (no key): real GDP growth for India and the US."""
    import asyncio

    from invest_agent.mcp_servers.macro_data import imf
    data = asyncio.run(imf.indicator("NGDP_RPCH"))
    assert "IND" in data and "USA" in data
