"""
agents/fundamentals.py: the Fundamentals Agent node.

What this file does:
    Plain code, no model call (design 14.3). Once the company is resolved, it makes one
    MCP call to filings_server's get_fundamentals tool and turns the answer into
    SourceRecords, the shape every number travels in:

      raw figures   one record per figure per fiscal year, e.g.
                    "sec_edgar:AAPL:revenue:FY2025" or "trendlyne:RELIANCE:net_income:FY2026",
                    carrying the fiscal year, period end, unit, SEC accession number (US)
                    and source tier
      measures      margins, ROE, ROIC, debt ratios, free cash flow, revenue growth,
                    recorded as "computed" with the IDs of the raw records they came
                    from, so any number in the final report can be traced back

    It also sets bank_mode, Trendlyne's sector label and industry in the state for later steps.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from invest_agent.common.records import make_record, make_record_id
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool

logger = logging.getLogger(__name__)

FetchFn = Callable[[str, str, str | None, int | None, str | None], Awaitable[dict]]


async def fetch_from_server(market: str, ticker: str, cik: str | None, sic: int | None,
                            company_name: str | None = None) -> dict:
    # company_name (10 October): lets Trendlyne find a company whose NSE code its search
    # does not match (NESTLEIND).
    return await call_tool("filings", "get_fundamentals",
                           {"market": market, "ticker": ticker, "cik": cik, "sic": sic,
                            "company_name": company_name}, timeout=120)


def _source_of(row: dict, default: str) -> str:
    """Capex rows for Indian companies come from yfinance even when the rest is Trendlyne."""
    return "yfinance" if str(row.get("tag", "")).startswith("yfinance") else default


def _row_flags(figure: str, row: dict, src: str) -> list[str] | None:
    """capex_from_yfinance (India), shares_outstanding_from_yfinance (US share classes), and
    "rebuilt" for operating income worked out from pre-tax income (7 October)."""
    flags = [f"{figure}_from_yfinance"] if src == "yfinance" else []
    if str(row.get("tag", "")).startswith(("rebuilt:", "derived:")):
        flags.append("rebuilt")
    return flags or None


def build_records(result: dict, ticker: str) -> list[dict]:
    source, tier = result.get("source") or "unknown", result.get("tier") or 2
    currency = result.get("currency")
    records = []

    for figure, rows in (result.get("figures") or {}).items():
        for row in rows:
            src = _source_of(row, source)
            unit = currency if row.get("unit") in ("USD", "INR") else row.get("unit")
            records.append(make_record(
                source=src, tier=2 if src != source else tier, entity=ticker, field=figure,
                value=row["value"], as_of=row.get("filed") or row["period_end"],
                period=f"FY{row['fiscal_year']}", unit=unit, fiscal_year=row["fiscal_year"],
                period_end=row["period_end"], accession_number=row.get("accession"),
                source_ref=row.get("tag"), flags=_row_flags(figure, row, src),
            ))

    figures = result.get("figures") or {}
    for measure, rows in (result.get("metrics") or {}).items():
        for row in rows:
            period = f"FY{row['fiscal_year']}"
            # An input is a raw figure (traced to its source) or another computed measure
            # (EBIT built from EBITDA, total debt built from its two parts).
            inputs = [make_record_id(source if name in figures else "computed", ticker, name, period)
                      for name in row.get("inputs", [])]
            records.append(make_record(
                source="computed", tier=tier, entity=ticker, field=measure, value=row["value"],
                as_of=f"{row['fiscal_year']}", period=period, fiscal_year=row["fiscal_year"],
                source_ref=",".join(inputs),
            ))
    return records


def make_fundamentals_node(fetch: FetchFn = fetch_from_server):
    async def fundamentals(state: GraphState) -> dict:
        ticker, market = state.get("ticker"), state.get("market")
        if not ticker or not market:
            return {"fundamentals_records": []}

        result = await fetch(market, ticker, state.get("cik"), state.get("sic_code"), state.get("company_name"))
        if result.get("flags"):
            logger.info("Fundamentals flags for %s: %s", ticker, result["flags"])

        update: dict = {"fundamentals_records": build_records(result, ticker),
                        "bank_mode": bool(result.get("bank_mode"))}
        # "sector" itself is set by the Geographic and Sector agent, which runs at the same
        # time; two parallel agents may not write the same field, so this keeps Trendlyne's.
        if result.get("sector"):
            update["trendlyne_sector"] = result["sector"]
        if result.get("industry"):
            update["industry"] = result["industry"]
        return update

    return fundamentals


fundamentals_node = make_fundamentals_node()
