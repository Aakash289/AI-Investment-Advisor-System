"""
mcp_servers/market_data/server.py: market_data_server, the MCP server behind the price agent.

What this file does:
    Offers three tools:

      get_price_history(symbol, market)   five years of daily prices from Yahoo (two from Massive)
      get_quote(symbol, market)           the last actual price and its date
      get_price_metrics(symbol, market)   everything the price agent needs in one call:
                                          the stock's history, its benchmark's history,
                                          and the figures computed by metrics.py

    Source order (design 14.4): Yahoo Finance first; if it errors, times out or
    returns nothing, Massive's free plan for US stocks. The Indian fallback,
    Trendlyne, is added in build step 5; until then an Indian stock with no Yahoo
    data returns an error flag instead of prices.

    Every answer says which source it came from, so the records downstream carry it.

Run it by itself (for debugging) with:
    uv run python -m invest_agent.mcp_servers.market_data.server
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from invest_agent.common.logging_setup import setup_logging
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.mcp_servers.market_data import massive, yahoo
from invest_agent.mcp_servers.market_data.metrics import compute_price_metrics
from invest_agent.observability.tracked_call import tracked_call

logger = logging.getLogger(__name__)

mcp = FastMCP("market_data_server")


async def history_with_fallback(symbol: str, market: str) -> dict:
    """{"symbol", "source", "rows", "flags"}; rows is empty if every source failed."""
    flags: list[str] = []
    try:
        # yahoo.fetch_history is an ordinary function; tracked_call runs it in a thread.
        rows = await tracked_call("yfinance", "history", yahoo.fetch_history, symbol, timeout=20)
        return {"symbol": symbol, "source": "yfinance", "rows": rows, "flags": flags}
    except Exception as err:
        logger.warning("Yahoo failed for %s: %s", symbol, err)
        flags.append("yahoo_failed")

    if market == "US" and not symbol.startswith("^"):
        try:
            rows = await tracked_call("massive", "aggregates", massive.fetch_history, symbol, timeout=20)
            return {"symbol": symbol, "source": "massive", "rows": rows, "flags": flags + ["used_fallback"]}
        except Exception as err:
            logger.warning("Massive failed for %s: %s", symbol, err)
            flags.append("massive_failed")
    elif market == "IN":
        flags.append("india_fallback_not_built_yet")    # Trendlyne prices, build step 5

    return {"symbol": symbol, "source": None, "rows": [], "flags": flags}


@mcp.tool()
async def get_price_history(symbol: str, market: str) -> dict:
    """Up to five years of daily prices for a symbol (Massive's free plan: two).

    symbol: the Yahoo-style symbol, e.g. "AAPL", "RELIANCE.NS", "^GSPC".
    market: "US" or "IN", which decides the fallback source.
    Returns {symbol, source, rows: [{date, close, adj_close, volume}], flags}.
    """
    return await history_with_fallback(symbol, market)


@mcp.tool()
async def get_quote(symbol: str, market: str) -> dict:
    """The last actual traded price for a symbol, with its date and source."""
    hist = await history_with_fallback(symbol, market)
    if not hist["rows"]:
        return {"symbol": symbol, "price": None, "as_of": None, "source": None, "flags": hist["flags"]}
    last = hist["rows"][-1]
    return {"symbol": symbol, "price": last["close"], "as_of": last["date"],
            "source": hist["source"], "flags": hist["flags"]}


async def price_metrics_logic(symbol: str, market: str, currency: str) -> dict:
    """The work behind get_price_metrics, separate so tests can call it directly."""
    stock = await history_with_fallback(symbol, market)
    if not stock["rows"]:
        return {"symbol": symbol, "source": None, "metrics": {}, "flags": stock["flags"] + ["no_price_data"]}

    benchmark_symbol = load_yaml("mcp_servers/market_data/benchmarks.yaml").get(market)
    bench_rows = None
    flags = list(stock["flags"])
    if benchmark_symbol:
        bench = await history_with_fallback(benchmark_symbol, market)
        bench_rows = bench["rows"] or None
        flags += [f"benchmark_{f}" for f in bench["flags"]]

    result = compute_price_metrics(stock["rows"], bench_rows, currency)
    return {
        "symbol": symbol,
        "source": stock["source"],
        "benchmark": benchmark_symbol,
        "metrics": result["metrics"],
        "flags": flags + result["flags"],
        "history_days": len(stock["rows"]),
        "chart": result.get("chart"),
    }


@mcp.tool()
async def get_price_metrics(symbol: str, market: str, currency: str) -> dict:
    """Price, 52-week range, returns, moving averages, volatility, beta, drawdown and
    liquidity for one stock, against its market's benchmark (S&P 500 or Nifty 50).

    symbol: the Yahoo-style symbol ("AAPL", "RELIANCE.NS"); market: "US" or "IN";
    currency: the listing currency ("USD" or "INR"), used for the thin-trading line.
    """
    return await price_metrics_logic(symbol, market, currency)


if __name__ == "__main__":
    setup_logging()
    mcp.run()
