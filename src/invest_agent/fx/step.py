"""
fx/step.py: the currency normalisation node in the graph (diagram node FX1).

What this file does:
    Runs once both retrieval agents have finished. Plain code, no model call.

      same currency      the common case (an Indian user, an Indian stock): nothing to
                         convert, no outside call, no risk note
      different currency asks fx_server for today's rate between the stock's currency
                         and the user's, converts the budget into the stock's currency
                         (conversion point 1, used for sizing in step 11), asks for the
                         12-month series and writes the currency risk note

    The rate itself becomes a SourceRecord, like every other number, so the report can
    cite it. If no rate can be found the request carries on: amounts stay in the stock's
    currency and the output says so (design 14.8: never convert at a guessed rate).

    Conversion point 2 (valuation for companies that report in a different currency)
    is done by the valuation step in build step 7, with fx/convert.py's convert_figures.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from invest_agent.common.records import make_record
from invest_agent.fx.convert import ABROAD_COSTS_NOTE, INDICATIVE_NOTE, convert
from invest_agent.fx.risk_note import risk_note
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool

logger = logging.getLogger(__name__)

RateFn = Callable[[str, str], Awaitable[dict]]
SeriesFn = Callable[[str, str, int], Awaitable[dict]]


async def rate_from_server(base: str, quote: str) -> dict:
    return await call_tool("fx", "get_rate", {"base": base, "quote": quote}, timeout=60)


async def series_from_server(base: str, quote: str, months: int) -> dict:
    return await call_tool("fx", "get_rate_series", {"base": base, "quote": quote, "months": months}, timeout=60)


def make_fx_node(get_rate: RateFn = rate_from_server, get_series: SeriesFn = series_from_server):
    async def currency_normalisation(state: GraphState) -> dict:
        profile = state.get("user_profile") or {}
        stock_ccy = state.get("listing_currency")
        user_ccy = profile.get("budget_currency")
        if not stock_ccy or not user_ccy or stock_ccy == user_ccy:
            return {"fx": {"needed": False}}

        fx: dict = {"needed": True, "stock_currency": stock_ccy, "user_currency": user_ccy,
                    "notes": [INDICATIVE_NOTE, ABROAD_COSTS_NOTE], "flags": []}
        # The rate is asked as user currency per unit of stock currency (rupees per
        # dollar), the way people in India usually quote it.
        try:
            rate = await get_rate(stock_ccy, user_ccy)
        except Exception as err:                     # the FX server itself failed
            logger.warning("FX server failed: %s", err)
            rate = {"rate": None, "flags": ["fx_server_failed"]}
        fx["rate"] = rate
        fx["flags"] += rate.get("flags", [])

        if rate.get("rate") is None:
            if "fx_unavailable" not in fx["flags"]:
                fx["flags"].append("fx_unavailable")
            fx["notes"].append(f"No exchange rate could be found, so amounts are given in {stock_ccy} only.")
            return {"fx": fx, "fx_flags": fx["flags"]}

        records = [make_record(
            source=rate["source"], tier=rate.get("tier") or 2, entity=f"{stock_ccy}{user_ccy}", field="fx_rate",
            value=rate["rate"], as_of=rate["date"], unit=f"{user_ccy} per {stock_ccy}",
            source_ref=rate.get("provider"), flags=[f for f in rate.get("flags", []) if f != "from_cache"] or None,
        )]

        if profile.get("budget_amount"):
            fx["budget_converted"] = convert(profile["budget_amount"], user_ccy, stock_ccy, rate)

        try:
            series = await get_series(stock_ccy, user_ccy, 12)
            fx["risk_note"] = risk_note(series.get("points", []), rate, user_ccy, stock_ccy)
            fx["flags"] += series.get("flags", [])
        except Exception as err:
            logger.warning("FX series failed: %s", err)
            fx["flags"].append("fx_series_unavailable")

        return {"fx": fx, "fx_records": records, "fx_flags": fx["flags"]}

    return currency_normalisation


fx_node = make_fx_node()
