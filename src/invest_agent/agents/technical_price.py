"""
agents/technical_price.py: the Technical and Price node.

What this file does:
    Plain code, no model call (design 14.4). Once the company is resolved, it asks
    market_data_server for the stock's price figures (one MCP call to
    get_price_metrics) and turns each figure into a SourceRecord, the shape every
    retrieved number travels in, so later steps (valuation, sizing, the report and the
    grounding guardrail) can always trace a number back to its source and date.

    It is descriptive only: price, range, returns, averages, volatility, beta,
    drawdown and liquidity. No trading signals and no price prediction.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from invest_agent.common.records import make_record
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool

logger = logging.getLogger(__name__)

# Units for each figure, so the report and the FX step know what each number is.
UNITS = {
    "current_price": "currency", "high_52w": "currency", "low_52w": "currency",
    "ma_50d": "currency", "ma_200d": "currency", "avg_traded_value_20d": "currency",
    "avg_traded_value_3m": "currency", "position_in_52w_range": "fraction",
    "return_1m": "fraction", "return_3m": "fraction", "return_6m": "fraction",
    "return_12m": "fraction", "volatility_1y": "fraction", "max_drawdown_2y": "fraction",
    "return_3y": "fraction", "return_5y": "fraction", "max_drawdown_5y": "fraction", "price_cagr_5y": "fraction",
    "benchmark_return_5y": "fraction", "benchmark_cagr_5y": "fraction",
    "beta": "ratio", "beta_weeks": "weeks", "avg_volume_20d": "shares", "avg_volume_6m": "shares",
    "thinly_traded": "flag",
}
SKIP = {"price_as_of", "currency"}       # carried on every record instead of as their own records

FetchFn = Callable[[str, str, str], Awaitable[dict]]


async def fetch_from_server(symbol: str, market: str, currency: str) -> dict:
    return await call_tool("market_data", "get_price_metrics",
                           {"symbol": symbol, "market": market, "currency": currency})


def yahoo_symbol(ticker: str, market: str) -> str:
    return f"{ticker}.NS" if market == "IN" else ticker


def make_technical_price_node(fetch: FetchFn = fetch_from_server):
    async def technical_price(state: GraphState) -> dict:
        ticker, market = state.get("ticker"), state.get("market")
        currency = state.get("listing_currency", "USD")
        if not ticker or not market:
            return {"price_records": []}

        result = await fetch(yahoo_symbol(ticker, market), market, currency)
        metrics = result.get("metrics") or {}
        source = result.get("source") or "unknown"
        as_of = metrics.get("price_as_of")
        if result.get("flags"):
            logger.info("Price data flags for %s: %s", ticker, result["flags"])

        records = []
        for field, value in metrics.items():
            if field in SKIP or value is None:
                continue                     # a missing figure stays missing
            unit = currency if UNITS.get(field) == "currency" else UNITS.get(field, "number")
            records.append(make_record(
                source=source, tier=2, entity=ticker, field=field, value=value,
                as_of=as_of, period=as_of, unit=unit,
                flags=[f for f in result.get("flags", []) if f == "used_fallback"] or None,
            ))
        # The weekly price series for the report's chart (not records: up to 260 points).
        update = {"price_records": records}
        if result.get("chart"):
            update["price_chart"] = {**result["chart"], "benchmark_symbol": result.get("benchmark"),
                                     "source": source}
        return update

    return technical_price


technical_price_node = make_technical_price_node()
