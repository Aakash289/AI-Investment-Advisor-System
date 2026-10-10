"""
agents/macro.py: the Macro Economy Agent node (design 14.5).

What this file does:
    Plain code, no model call. It makes one MCP call to macro_data_server's
    get_macro_panel tool, which returns the six panel economies (US, India, China,
    Eurozone, Japan, UK) with their indicators and regime labels, cached for a day and
    shared by every request. It then:

      turns every indicator into a SourceRecord, e.g. "fred:US:inflation:2026-08-01" or
      "bis:IN:policy_rate:2026-07-22", so any figure in the report can be traced
      adds each regime label as a record too ("computed:IN:regime_rates"), listing the
      indicator it came from
      keeps the whole panel in the state (macro_panel) for Macro Context Analysis (F2)
      in step 9, which weighs each country by the company's revenue there

    The company's home economy is the US for US listings and India for Indian ones.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from invest_agent.common.records import make_record
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool

logger = logging.getLogger(__name__)

PanelFn = Callable[[], Awaitable[dict]]
TIER = {"fred": 1, "bis": 1, "imf": 1, "world_bank": 1}
UNITS = {"policy_rate": "percent", "bond_10y": "percent", "yield_curve": "percentage points", "inflation": "percent",
         "gdp_growth": "percent", "industrial_production": "percent", "unemployment": "percent",
         "credit_spread": "percentage points"}
REGIME_INPUT = {"rates": "policy_rate", "inflation": "inflation", "growth": "gdp_growth",
                "credit": "credit_spread", "yield_curve": "yield_curve"}


async def panel_from_server() -> dict:
    return await call_tool("macro_data", "get_macro_panel", {}, timeout=120)


def build_records(panel: dict) -> list[dict]:
    records = []
    for code, country in (panel.get("countries") or {}).items():
        ids = {}
        for name, ind in (country.get("indicators") or {}).items():
            if ind.get("value") is None:
                continue
            period = str(ind.get("date"))
            r = make_record(
                source=ind.get("source", "unknown"), tier=TIER.get(ind.get("source"), 2), entity=code, field=name,
                value=ind["value"], as_of=period, period=period, unit=UNITS.get(name, "number"),
                source_ref=ind.get("series"),
                flags=[f for f in ("stale", "estimate", "lagged") if ind.get(f)] or None)
            records.append(r)
            ids[name] = r["record_id"]
        for regime, label in (country.get("regimes") or {}).items():
            records.append(make_record(source="computed", tier=1, entity=code, field=f"regime_{regime}", value=label,
                                       as_of=panel.get("as_of"), source_ref=ids.get(REGIME_INPUT[regime])))
    return records


def make_macro_node(fetch: PanelFn = panel_from_server):
    async def macro(state: GraphState) -> dict:
        try:
            panel = await fetch()
        except Exception as err:                       # the server itself failed
            logger.warning("Macro panel failed: %s", err)
            return {"macro_records": [], "macro_panel": {"countries": {}, "flags": ["macro_failed"]}}
        if panel.get("flags"):
            logger.info("Macro panel flags: %s", panel["flags"])
        home = "US" if state.get("market") == "US" else "IN"
        return {"macro_records": build_records(panel), "macro_panel": {**panel, "home": home}}

    return macro


macro_node = make_macro_node()
