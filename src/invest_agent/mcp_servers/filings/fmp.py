"""
mcp_servers/filings/fmp.py: the US fallback for fundamentals, Financial Modeling Prep's free plan.

What this file does:
    When SEC's file cannot be fetched or is missing critical figures, this fetches the
    same figures from FMP's free plan (250 calls a day) and returns them in the same
    shape normaliser.py produces, so nothing downstream needs to know which source
    answered. Three calls per company: income statement, balance sheet, cash flow.

    FMP mostly takes its figures from the same SEC filings, so it is a backup for when
    SEC is unreachable, not a second opinion (design 14.3), and it is Tier 2.
"""

from __future__ import annotations

import httpx

from invest_agent.config import get_settings

BASE = "https://financialmodelingprep.com/stable"
YEARS = 6
FREE_PLAN_LIMIT = 5      # the free plan returns at most 5 annual years; asking for more is refused

# Our figure name -> FMP field names to try, in order (FMP has renamed some fields).
FIELDS = {
    "income-statement": {
        "revenue": ["revenue"], "cost_of_revenue": ["costOfRevenue"],
        "operating_income": ["operatingIncome"], "net_income": ["netIncome"],
        "eps_diluted": ["epsDiluted", "epsdiluted"], "interest_expense": ["interestExpense"],
        "income_tax": ["incomeTaxExpense"], "depreciation_amortization": ["depreciationAndAmortization"],
    },
    "balance-sheet-statement": {
        "total_equity": ["totalStockholdersEquity"], "long_term_debt": ["longTermDebt"],
        "short_term_debt": ["shortTermDebt"], "cash": ["cashAndCashEquivalents"],
        "total_assets": ["totalAssets"],
    },
    "cash-flow-statement": {
        "operating_cash_flow": ["operatingCashFlow", "netCashProvidedByOperatingActivities"],
        "capex": ["capitalExpenditure"], "dividends_paid": ["commonDividendsPaid", "dividendsPaid"],
        "buybacks": ["commonStockRepurchased"],
    },
}
UNITS = {"eps_diluted": "USD/shares"}
# FMP reports money paid out as negative numbers; SEC's tags for these are positive.
PAID_OUT = {"capex", "dividends_paid", "buybacks"}


class FmpNotConfigured(RuntimeError):
    """FINANCIAL_MODELING_PREP_API_KEY is missing from .env."""


def map_statements(statements: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Turn FMP's three statement lists into {figure: [year rows, newest first]}."""
    out: dict[str, list[dict]] = {}
    for statement, fields in FIELDS.items():
        for row in statements.get(statement, [])[:YEARS]:
            end = row.get("date")
            year = row.get("fiscalYear") or row.get("calendarYear") or (end or "")[:4]
            for figure, names in fields.items():
                value = next((row[n] for n in names if row.get(n) is not None), None)
                if value is None or end is None:
                    continue
                value = abs(float(value)) if figure in PAID_OUT else float(value)
                out.setdefault(figure, []).append({
                    "fiscal_year": int(year), "period_end": end, "value": value,
                    "unit": UNITS.get(figure, "USD"), "tag": f"fmp:{statement}", "accession": None,
                    "filed": row.get("filingDate") or row.get("fillingDate"),
                })
    for rows in out.values():
        rows.sort(key=lambda r: r["fiscal_year"], reverse=True)
    return out


async def fetch_statements(ticker: str) -> dict[str, list[dict]]:
    """The three annual statements for a US ticker, then mapped to our figures."""
    key = get_settings().financial_modeling_prep_api_key
    if not key:
        raise FmpNotConfigured("FINANCIAL_MODELING_PREP_API_KEY is not set in .env")
    statements = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for statement in FIELDS:
            response = await client.get(f"{BASE}/{statement}",
                                        params={"symbol": ticker, "period": "annual", "limit": FREE_PLAN_LIMIT, "apikey": key})
            response.raise_for_status()
            statements[statement] = response.json() or []
    return map_statements(statements)