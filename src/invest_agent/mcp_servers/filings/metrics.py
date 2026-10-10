"""
mcp_servers/filings/metrics.py: the figures built from the raw annual statements.

What this file does:
    From the six years of raw figures (revenue, profit, cash flow, equity, debt...) it
    builds the measures the analysis scores, year by year where the inputs exist
    (design 6.3 and 14.3):

      gross, operating and net margin      profit as a share of revenue
      EBITDA                               operating income + depreciation (if not given)
      ROE                                  net income / shareholders' equity
      ROIC                                 after-tax operating income / capital invested
                                           (equity + debt - cash)
      debt to equity                       (long + short term debt) / equity
      net debt and net debt to EBITDA      debt minus cash, and how many years of EBITDA
                                           it would take to repay
      interest coverage                    operating income / interest
      free cash flow                       cash from operations - capex
      revenue growth (CAGR)                the steady yearly growth rate over the years held

    Bank mode (banks, NBFCs, insurers) skips gross margin, EBITDA, debt ratios, interest
    coverage and free cash flow, which do not mean the same thing for a lender.

    Every result says which raw figures it came from, so the grounding guardrail can
    trace any number back. A measure that would divide by zero or by a negative base
    is left out, never guessed.
"""

from __future__ import annotations

from typing import Any

BANK_SKIP = {"gross_margin", "ebitda", "debt_to_equity", "net_debt", "net_debt_to_ebitda",
             "interest_coverage", "free_cash_flow", "roic"}


def _by_year(rows: list[dict] | None) -> dict[int, float]:
    return {r["fiscal_year"]: r["value"] for r in (rows or [])}


def _ratio(a: float | None, b: float | None, positive_base: bool = True) -> float | None:
    if a is None or b is None or b == 0 or (positive_base and b < 0):
        return None
    return a / b


def compute_metrics(figures: dict[str, list[dict]], bank_mode: bool = False) -> dict[str, list[dict]]:
    """Return {measure: [{fiscal_year, value, inputs}], newest first}."""
    f = {name: _by_year(rows) for name, rows in figures.items()}
    years = sorted(set().union(*[set(v) for v in f.values()]) if f else set(), reverse=True)
    out: dict[str, list[dict]] = {}

    def add(name: str, year: int, value: float | None, inputs: list[str]) -> None:
        if value is None or (bank_mode and name in BANK_SKIP):
            return
        out.setdefault(name, []).append({"fiscal_year": year, "value": round(value, 6), "inputs": inputs})

    def g(name: str, year: int) -> float | None:
        return f.get(name, {}).get(year)

    for y in years:
        rev, ni = g("revenue", y), g("net_income", y)
        op = g("operating_income", y)
        da = g("depreciation_amortization", y)
        ebitda = g("ebitda", y)                                # given directly for Indian companies
        if op is None and ebitda is not None and da is not None:
            op = ebitda - da                                   # Indian figures give EBITDA, not EBIT
            add("operating_income", y, op, ["ebitda", "depreciation_amortization"])
        if ebitda is None and op is not None and da is not None:
            ebitda = op + da                                   # US figures give EBIT, not EBITDA
            add("ebitda", y, ebitda, ["operating_income", "depreciation_amortization"])

        cost = g("cost_of_revenue", y)
        if rev is not None and cost is not None:
            add("gross_margin", y, _ratio(rev - cost, rev), ["revenue", "cost_of_revenue"])
        add("operating_margin", y, _ratio(op, rev), ["operating_income", "revenue"])
        add("net_margin", y, _ratio(ni, rev), ["net_income", "revenue"])

        equity = g("total_equity", y)
        add("roe", y, _ratio(ni, equity), ["net_income", "total_equity"])

        lt, st, cash = g("long_term_debt", y), g("short_term_debt", y), g("cash", y)
        debt = None if lt is None and st is None else (lt or 0.0) + (st or 0.0)
        if debt is not None:
            add("total_debt", y, debt, ["long_term_debt", "short_term_debt"])
            add("debt_to_equity", y, _ratio(debt, equity), ["total_debt", "total_equity"])
            if cash is not None:
                add("net_debt", y, debt - cash, ["total_debt", "cash"])
                add("net_debt_to_ebitda", y, _ratio(debt - cash, ebitda), ["net_debt", "ebitda"])

        tax = g("income_tax", y)
        if op is not None and equity is not None and debt is not None and cash is not None:
            rate = 0.25                                         # used when the tax rate cannot be worked out
            if tax is not None and ni is not None and (ni + tax) > 0:
                rate = min(max(tax / (ni + tax), 0.0), 0.5)
            add("roic", y, _ratio(op * (1 - rate), equity + debt - cash),
                ["operating_income", "income_tax", "net_income", "total_equity", "total_debt", "cash"])

        add("interest_coverage", y, _ratio(op, g("interest_expense", y)), ["operating_income", "interest_expense"])

        ocf, capex = g("operating_cash_flow", y), g("capex", y)
        if ocf is not None and capex is not None:
            add("free_cash_flow", y, ocf - capex, ["operating_cash_flow", "capex"])

    # Revenue growth: steady yearly rate from the oldest to the newest year held.
    revenue = sorted(f.get("revenue", {}).items())
    if len(revenue) >= 2:
        (y0, v0), (y1, v1) = revenue[0], revenue[-1]
        if v0 > 0 and v1 > 0 and y1 > y0:
            add("revenue_cagr", y1, (v1 / v0) ** (1 / (y1 - y0)) - 1, ["revenue"])
            out["revenue_cagr"][-1]["years"] = y1 - y0

    return out