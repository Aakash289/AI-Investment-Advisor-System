"""
mcp_servers/filings/yahoo_cashflow.py: capex for Indian companies, from yfinance's annual cash flow.

What this file does:
    Trendlyne has no capex history (checked 5 October), and free cash flow needs it:
    free cash flow = cash from operations - capex. yfinance's annual cash flow statement
    for NSE stocks carries "Capital Expenditure" for about four years, so this file
    reads it (design 14.3, decision of 5 October). It is Tier 2 and every value is
    flagged, so the report can say where it came from.

    yfinance reports capex as a negative number in rupees; it is returned here as a
    positive amount paid out, the same way SEC's capex tag is.

    fetch_implied_shares (7 October): the share count for US companies whose SEC file has
    none usable, such as Alphabet with its three share classes.
"""

from __future__ import annotations

import yfinance as yf


def fetch_capex(symbol: str) -> list[dict]:
    """[{fiscal_year, period_end, value, unit, tag}] newest first; empty if not available.

    yf.Ticker(symbol).cashflow is a table with one row per line item and one column per
    fiscal year end date.
    """
    table = yf.Ticker(symbol).cashflow
    if table is None or table.empty or "Capital Expenditure" not in table.index:
        return []
    rows = []
    for when, value in table.loc["Capital Expenditure"].items():
        if value is None or value != value:             # skip missing years (NaN)
            continue
        rows.append({
            "fiscal_year": when.year, "period_end": when.date().isoformat(), "value": abs(float(value)),
            "unit": "INR", "tag": "yfinance:Capital Expenditure", "accession": None, "filed": None,
        })
    rows.sort(key=lambda r: r["fiscal_year"], reverse=True)
    return rows


def fetch_implied_shares(symbol: str) -> float | None:
    """All share classes together, as Yahoo implies them (market value / price), for US
    companies whose SEC file has no usable share count (Alphabet, 7 October). Tier 2."""
    info = yf.Ticker(symbol).info or {}
    implied = info.get("impliedSharesOutstanding")
    if implied:
        return float(implied)
    cap, price = info.get("marketCap"), info.get("currentPrice") or info.get("regularMarketPrice")
    return float(cap) / float(price) if cap and price else None
