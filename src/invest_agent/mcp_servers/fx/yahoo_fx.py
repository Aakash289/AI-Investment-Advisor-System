"""
mcp_servers/fx/yahoo_fx.py: the market exchange rate from Yahoo Finance, the last resort.

What this file does:
    Yahoo quotes currency pairs as symbols such as "USDINR=X" (rupees per dollar). It is
    a live market rate, not an official reference rate, and yfinance is unofficial, so
    it is only used when both Frankfurter and FRED have failed (design 14.8), and the
    answer is flagged as unofficial.

    yfinance is an ordinary (not async) library; the server calls this function through
    tracked_call, which runs it in a worker thread.
"""

from __future__ import annotations

import yfinance as yf


class EmptyFxHistory(RuntimeError):
    """Yahoo answered but had no recent rate for the pair."""


def latest_rate(base: str, quote: str) -> dict:
    """{"rate", "date"}: the last daily close in the past week, quote per base."""
    table = yf.Ticker(f"{base}{quote}=X").history(period="5d", interval="1d")
    if table is None or table.empty:
        raise EmptyFxHistory(f"Yahoo returned no {base}{quote}=X rate")
    closes = table["Close"].dropna()            # dropna removes days with no value
    if closes.empty:
        raise EmptyFxHistory(f"Yahoo returned only empty {base}{quote}=X rows")
    return {"rate": float(closes.iloc[-1]), "date": closes.index[-1].date().isoformat()}
