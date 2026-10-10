"""
agents/geo_sector.py: the Geographic and Sector Exposure Agent node (design 14.7).

What this file does:
    Plain code, no model call. One MCP call to filings_server's get_geo_sector, then:

      geography   the share of revenue from each panel economy (US, India, China,
                  Eurozone, Japan, UK) and the rest of the world. US companies: their
                  latest 10-K; Indian companies: the "within India / outside India" table
                  in their annual report. If neither gives a usable breakdown, 100% home
                  country, flagged. Macro Context Analysis (F2, step 9) weighs each
                  economy's conditions by these shares.
      sector      one of our 11 sectors (SIC code for US companies, Yahoo for Indian ones),
                  used by the sector sensitivity table in F2

    Each share becomes a record ("sec_10k:AAPL:revenue_share_CN:FY2025"), as does the
    sector, so the report can say where they came from.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from invest_agent.common.records import make_record
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool

logger = logging.getLogger(__name__)

GeoFn = Callable[[str, str, str | None, int | None], Awaitable[dict]]
SOURCE_TIER = {"sec_10k": 1, "trendlyne_annual_report": 2, "home_default": 3}


async def geo_from_server(market: str, ticker: str, company_name: str | None, sic: int | None) -> dict:
    return await call_tool("filings", "get_geo_sector", {"market": market, "ticker": ticker,
                                                         "company_name": company_name, "sic": sic}, timeout=180)


def build_records(result: dict, ticker: str) -> list[dict]:
    geo = result.get("geography") or {}
    source = geo.get("source", "unknown")
    period = f"FY{geo['fiscal_period_end'][:4]}" if geo.get("fiscal_period_end") else None
    records = [make_record(source=source, tier=SOURCE_TIER.get(source, 2), entity=ticker, field=f"revenue_share_{code}",
                           value=share, as_of=geo.get("fiscal_period_end"), period=period, unit="ratio",
                           accession_number=geo.get("accession"), source_ref=geo.get("axis"),
                           flags=None if geo.get("reliable") else ["home_default"])
               for code, share in (geo.get("weights") or {}).items()]
    s = result.get("sector") or {}
    if s.get("sector"):
        records.append(make_record(source=s.get("source") or "unknown", tier=1 if s.get("source") == "sic" else 2,
                                   entity=ticker, field="sector", value=s["sector"],
                                   source_ref=f"SIC {s.get('sic')}" if s.get("source") == "sic" else s.get("yahoo_sector")))
    return records


def make_geo_sector_node(fetch: GeoFn = geo_from_server):
    async def geo_sector(state: GraphState) -> dict:
        ticker, market = state.get("ticker"), state.get("market")
        if not ticker or not market:
            return {"geo_sector_records": []}
        try:
            result = await fetch(market, ticker, state.get("company_name"), state.get("sic_code"))
        except Exception as err:                       # the server itself failed
            logger.warning("Geography and sector failed: %s", err)
            home = "US" if market == "US" else "IN"
            return {"geo_sector_records": [], "geo_exposure": {"weights": {home: 1.0}, "source": "home_default",
                                                              "reliable": False, "flags": ["geo_sector_failed"]}}
        update: dict = {"geo_sector_records": build_records(result, ticker),
                        "geo_exposure": {**(result.get("geography") or {}), "flags": result.get("flags", []),
                                         "sector_detail": result.get("sector")}}
        if (result.get("sector") or {}).get("sector"):
            update["sector"] = result["sector"]["sector"]
        return update

    return geo_sector


geo_sector_node = make_geo_sector_node()
