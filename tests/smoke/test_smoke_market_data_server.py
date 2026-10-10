"""
Smoke test: market/price data, the Tier 2 technical source.

yfinance needs no API key. This confirms the library can actually reach Yahoo Finance
from this environment before any agent logic is built on top of it.

Run with: uv run pytest tests/smoke/test_smoke_market_data_server.py -v
"""

import pytest
import yfinance as yf


@pytest.mark.smoke
def test_yfinance_price_lookup():
    ticker = yf.Ticker("AAPL")
    hist = ticker.history(period="5d")
    assert not hist.empty
    assert "Close" in hist.columns
