"""
Smoke test: Financial Modeling Prep's free plan, the US fallback for fundamentals.

One call (of the free plan's 250 a day): Apple's latest annual income statement.

Run with: uv run pytest tests/smoke/test_smoke_fmp.py -v
"""

import httpx
import pytest

from invest_agent.config import get_settings


@pytest.mark.smoke
def test_fmp_income_statement():
    key = get_settings().financial_modeling_prep_api_key
    if not key:
        pytest.skip("FINANCIAL_MODELING_PREP_API_KEY not set in .env")
    response = httpx.get("https://financialmodelingprep.com/stable/income-statement",
                         params={"symbol": "AAPL", "period": "annual", "limit": 1, "apikey": key}, timeout=15)
    response.raise_for_status()
    rows = response.json()
    assert rows and rows[0]["revenue"] > 0
