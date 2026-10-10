"""
Unit tests for the price figures (mcp_servers/market_data/metrics.py), the server's
fallback logic, and the price agent node. Price histories are generated in the test,
including a 10-for-1 split like NVIDIA's in June 2024, so no network is needed. Run with:
    uv run pytest tests/unit/test_price_metrics.py -v
"""

import asyncio
import math
from datetime import date, timedelta

import pytest

from invest_agent.agents.technical_price import make_technical_price_node
from invest_agent.mcp_servers.market_data import server
from invest_agent.mcp_servers.market_data.metrics import compute_price_metrics


def make_rows(days=504, start_price=100.0, daily_growth=0.0005, wiggle=0.01, volume=1_000_000,
              split_day=None, split_ratio=10):
    """Business-day price rows. The price grows steadily with a small up-down wiggle.
    With split_day set, the actual close drops by split_ratio from that day on while the
    adjusted close carries on smoothly, exactly as a real split looks."""
    rows, d, price = [], date(2024, 1, 1), start_price
    for i in range(days):
        while d.weekday() >= 5:                   # skip Saturdays and Sundays
            d += timedelta(days=1)
        price *= 1 + daily_growth + (wiggle if i % 2 else -wiggle)
        adjusted = price / split_ratio if split_day is not None else price
        actual = price / split_ratio if (split_day is not None and i >= split_day) else price
        rows.append({"date": d.isoformat(), "close": actual, "adj_close": adjusted, "volume": volume})
        d += timedelta(days=1)
    return rows


def test_core_figures():
    rows = make_rows()
    out = compute_price_metrics(rows, make_rows(wiggle=0.008), "USD")
    m = out["metrics"]
    assert m["current_price"] == pytest.approx(rows[-1]["close"], rel=1e-6)
    assert m["return_12m"] > 0 and 0 <= m["position_in_52w_range"] <= 1
    assert 0.1 < m["volatility_1y"] < 0.2               # 1% daily wiggle is about 16% a year
    assert m["beta"] is not None and m["beta_weeks"] >= 52
    assert m["max_drawdown_2y"] <= 0


def test_split_does_not_look_like_a_crash():
    rows = make_rows(split_day=300)
    m = compute_price_metrics(rows, None, "USD")["metrics"]
    # The actual price fell to a tenth on day 300, but the adjusted history is smooth.
    assert m["max_drawdown_2y"] > -0.05
    assert m["return_12m"] > 0
    assert m["current_price"] == pytest.approx(rows[-1]["close"])   # today's real price


def test_short_history_flags_instead_of_guessing():
    out = compute_price_metrics(make_rows(days=40), make_rows(days=40), "INR")
    assert "under_one_year_of_history" in out["flags"] and "too_little_history_for_beta" in out["flags"]
    assert "beta" not in out["metrics"] and "high_52w" not in out["metrics"]


def test_thinly_traded_line_by_currency():
    # About Rs 100 x 10,000 shares = Rs 10 lakh a day, far below Rs 5 crore.
    thin = compute_price_metrics(make_rows(volume=10_000), None, "INR")["metrics"]
    assert thin["thinly_traded"] is True
    busy = compute_price_metrics(make_rows(volume=1_000_000), None, "USD")["metrics"]
    assert busy["thinly_traded"] is False                # about $100 million a day


def test_no_nan_values_reach_the_output():
    m = compute_price_metrics(make_rows(days=30), None, "USD")["metrics"]
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in m.values())


def test_server_falls_back_to_massive_for_us(monkeypatch):
    async def fake_tracked(service, op, fn, *args, **kwargs):
        if service == "yfinance":
            raise RuntimeError("empty table")
        return make_rows()
    # monkeypatch swaps a function for the length of one test, then puts it back.
    monkeypatch.setattr(server, "tracked_call", fake_tracked)
    out = asyncio.run(server.history_with_fallback("AAPL", "US"))
    assert out["source"] == "massive" and "used_fallback" in out["flags"]
    india = asyncio.run(server.history_with_fallback("RELIANCE.NS", "IN"))
    assert india["rows"] == [] and "india_fallback_not_built_yet" in india["flags"]


def test_price_node_makes_source_records():
    async def fake_fetch(symbol, market, currency):
        assert symbol == "RELIANCE.NS"
        return {"source": "yfinance", "flags": [],
                "metrics": {"current_price": 2950.5, "price_as_of": "2026-10-03", "currency": "INR",
                            "beta": 0.9, "return_1m": None}}
    node = make_technical_price_node(fake_fetch)
    out = asyncio.run(node({"ticker": "RELIANCE", "market": "IN", "listing_currency": "INR"}))
    records = {r["field"]: r for r in out["price_records"]}
    assert set(records) == {"current_price", "beta"}            # None stays missing
    assert records["current_price"]["record_id"] == "yfinance:RELIANCE:current_price:2026-10-03"
    assert records["current_price"]["unit"] == "INR" and records["current_price"]["tier"] == 2


def test_five_years_of_prices():
    """Five years of daily prices: 3- and 5-year changes, the 5-year growth rate and worst
    fall, the 2-year drawdown still over 2 years only, and weekly closes for the chart."""
    rows = make_rows(days=1300)
    bench = make_rows(days=1300, daily_growth=0.0003)
    out = compute_price_metrics(rows, bench, "INR")
    m = out["metrics"]
    assert m["return_5y"] is not None and m["return_3y"] is not None and m["price_cagr_5y"] is not None
    assert m["max_drawdown_5y"] <= m["max_drawdown_2y"] <= 0
    # The market index over the same five years, set beside the stock's growth.
    assert m["benchmark_cagr_5y"] is not None and m["benchmark_cagr_5y"] < m["price_cagr_5y"]
    chart = out["chart"]
    assert len(chart["dates"]) == len(chart["stock"]) <= 260 and chart["benchmark"] is not None
    assert chart["years"] == pytest.approx(1300 / 252, abs=0.1)


def test_two_years_gives_no_five_year_figures():
    m = compute_price_metrics(make_rows(days=504), None, "USD")["metrics"]
    assert "return_5y" not in m and "max_drawdown_5y" not in m


def test_five_year_figures_from_a_real_length_download():
    """A "5y" download holds about 1,245 trading days, not 1,260 (holidays). The 5-year
    change and growth rate must still be worked out from the price 5 years ago."""
    rows = make_rows(days=1306)                     # five calendar years of weekdays
    rows = [r for i, r in enumerate(rows) if i == 0 or i % 22]   # about 15 holidays a year
    assert len(rows) < 1260
    m = compute_price_metrics(rows, None, "INR")["metrics"]
    assert m["return_5y"] is not None and m["price_cagr_5y"] is not None


def test_chart_never_dated_after_the_last_price():
    """The last weekly point is dated by its real last trading day, not by the Friday
    that ends the week, so the chart never runs past today."""
    rows = make_rows(days=1300)
    rows = [r for r in rows if date.fromisoformat(str(r["date"])[:10]).weekday() != 4][:-1]  # end mid-week
    chart = compute_price_metrics(rows, None, "INR")["chart"]
    assert chart["dates"][-1] == str(rows[-1]["date"])[:10]
    assert chart["dates"] == sorted(chart["dates"])
