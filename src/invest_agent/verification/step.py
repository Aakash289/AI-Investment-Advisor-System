"""
verification/step.py: the verification node in the graph (diagram nodes E to E5).

What this file does:
    Runs once after the FX step, with every record the retrieval agents gathered.
    Plain code, no model call; its only outside call is the peer multiples. In order:

      1. cross_source   one value per figure and period, conflicts logged
      2. sanity         range, EPS, currency, freshness and continuity checks
      3. coverage       the small-company rule; below the limit the request ends here
                        with a "not covered in version 1" reply
      4. completeness   the score and the gate: proceed, lower confidence, or withhold
                        the amount (the verdict is always still given)
      5. valuation      the company's own multiples, then the peer medians (one call to
                        filings_server's get_peer_multiples, cached for a day)
      6. data notes     plain sentences for the report and, when data quality changed
                        the outcome, one line for the compact answer

    The records that come out (verified_records) are the only ones later steps use.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date

from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool
from invest_agent.verification import completeness, coverage, cross_source, data_notes, peers, sanity, valuation

logger = logging.getLogger(__name__)

PeersFn = Callable[[str, str, str | None], Awaitable[dict]]


async def peers_from_server(market: str, ticker: str, industry: str | None) -> dict:
    # A long timeout: the first US request of the day downloads several SEC frames
    # (one figure for every US company each); after that they come from the cache.
    return await call_tool("filings", "get_peer_multiples",
                           {"market": market, "ticker": ticker, "industry": industry}, timeout=240)


async def verify(state: GraphState, today: date | None = None, fetch_peers: PeersFn = peers_from_server) -> dict:
    ticker = state.get("ticker") or "UNKNOWN"
    listing = state.get("listing_currency")
    market = state.get("market")
    gathered = list(state.get("price_records", [])) + list(state.get("fundamentals_records", [])) \
        + list(state.get("fx_records", []))
    reporting = next((r.get("unit") for r in state.get("fundamentals_records", [])
                      if r["field"] == "revenue" and r.get("unit")), None)

    records, conflicts = cross_source.resolve(gathered)
    checked = sanity.check_records(records, ticker=ticker, listing_currency=listing,
                                   reporting_currency=reporting, today=today)
    records = checked["records"]

    cover = coverage.check_market_value(records, listing)
    if not cover["covered"]:
        name = state.get("company_name") or ticker
        return {"verified_records": records, "sanity_flags": checked["findings"], "conflicts": conflicts,
                "verdict": "not_covered", "not_covered_reason": cover["reason"],
                "final_response": f"{name} is not covered in version 1 because {cover['reason']}."}

    bank = bool(state.get("bank_mode"))
    val = valuation.own_multiples(records, ticker=ticker, bank_mode=bank,
                                  listing_currency=listing, reporting_currency=reporting)
    records = records + val.pop("records")
    val["market_value_check"] = {k: v for k, v in cover.items() if k != "covered"}

    if val["status"] == "ok" and market:
        try:
            peer_result = await fetch_peers(market, ticker, state.get("industry"))
        except Exception as err:                       # the server itself failed
            logger.warning("Peer multiples failed: %s", err)
            peer_result = {"peers": [], "flags": ["peers_failed"]}
        price = next((r for r in records if r["field"] == "current_price"), {})
        val["peers"] = peers.compare(val["multiples"], peer_result, ticker=ticker, market=market, bank_mode=bank,
                                     as_of=price.get("as_of"), year=val.get("year"),
                                     company_value=val["multiples"].get("market_value"), currency=listing)
        records = records + val["peers"].pop("records")

    profile = state.get("user_profile") or {}
    countries = (state.get("macro_panel") or {}).get("countries") or {}
    home = "US" if market == "US" else "IN"
    extra = {"budget": profile.get("budget_amount"), "sector": state.get("sector"),
             "peer_medians": bool((val.get("peers") or {}).get("comparisons")),
             "home_macro": len((countries.get(home) or {}).get("regimes") or {}) >= 2,
             "panel_macro": sum(1 for c, v in countries.items() if c != home and v.get("regimes")) >= 4,
             "news": ((state.get("news") or {}).get("summary") or {}).get("relevant_count", 0) > 0,
             "geographic_split": bool((state.get("geo_exposure") or {}).get("reliable"))}
    comp = completeness.score(records, market=market, bank_mode=bank, extra=extra)

    tier2_only = market == "IN" and any(r.get("source") == "trendlyne" for r in records)
    notes = data_notes.build_notes(checked["findings"], conflicts, comp)
    line = data_notes.user_line(comp, tier2_only)

    return {
        "verified_records": records,
        "sanity_flags": checked["findings"],
        "conflicts": conflicts,
        "completeness_score": comp["score"],
        "completeness_detail": {**comp, "unbroken_years": checked["unbroken_years"], "user_line": line},
        "amount_withheld": comp["decision"] == "withhold_amount",
        "amount_withheld_reason": comp["reason"] if comp["decision"] == "withhold_amount" else "",
        "valuation": val,
        "data_notes": notes,
    }


def make_verification_node(today: date | None = None, fetch_peers: PeersFn = peers_from_server):
    """The graph node. today and the peer source can be replaced in tests."""
    async def verification(state: GraphState) -> dict:
        return await verify(state, today, fetch_peers)
    return verification


verification_node = make_verification_node()
