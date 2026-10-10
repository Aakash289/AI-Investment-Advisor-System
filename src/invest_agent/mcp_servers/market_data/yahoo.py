"""
mcp_servers/market_data/yahoo.py: daily prices from Yahoo Finance, the primary price source.

What this file does:
    Fetches up to two years of daily prices for one symbol ("AAPL", "RELIANCE.NS",
    or an index such as "^GSPC") with the yfinance library, and returns them as plain
    rows: {date, close, adj_close, volume}.

    yfinance is free and needs no key, but it is unofficial and sometimes fails
    silently by returning an empty table instead of an error. An empty table is
    therefore treated as a failure here (design 14.4), so the server moves on to the
    fallback source.

    yfinance is an ordinary (not async) library, so the server calls it through
    tracked_call, which runs it in a worker thread with the usual timeout and retries.
"""

from __future__ import annotations

# yfinance downloads Yahoo Finance data. yf.Ticker(symbol).history(...) returns a
# pandas table with one row per trading day and columns Open, High, Low, Close,
# Adj Close, Volume, Dividends and Stock Splits.
import yfinance as yf


class EmptyPriceHistory(RuntimeError):
    """Yahoo answered but returned no rows (unknown symbol, delisted, or a silent failure)."""


def fetch_history(symbol: str, period: str = "5y") -> list[dict]:
    """Daily rows, oldest first. auto_adjust=False keeps both the actual close and the
    adjusted close, so the caller gets today's real price and a split-safe history."""
    table = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    if table is None or table.empty:
        raise EmptyPriceHistory(f"Yahoo returned no prices for {symbol}")

    rows = []
    # iterrows() walks the table one day at a time: the index is the date, the row the values.
    for when, row in table.iterrows():
        close = row.get("Close")
        if close is None or close != close:      # close != close is True only for NaN (missing)
            continue
        adj = row.get("Adj Close")
        rows.append({
            "date": when.date().isoformat(),
            "close": float(close),
            "adj_close": float(adj) if adj == adj and adj is not None else float(close),
            "volume": float(row.get("Volume") or 0),
        })
    if not rows:
        raise EmptyPriceHistory(f"Yahoo returned only empty rows for {symbol}")
    return rows
