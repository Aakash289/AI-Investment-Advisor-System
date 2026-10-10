"""
Unit tests for the graph wiring (graph/build_graph.py and graph/nodes.py).

Each agent node is replaced by a small fake, so these tests check only the wiring:
both input agents run, the gate stops or continues correctly, questions are combined,
and a not-covered company ends the run. No key, server or network needed. Run with:
    uv run pytest tests/unit/test_graph.py -v
"""

import asyncio

from invest_agent.graph.build_graph import build_graph

PROFILE = {"risk_tolerance": "low", "horizon_years": 10, "goal": "growth", "budget_amount": 200_000}


def fakes(entity: dict, intake: dict, calls: list):
    async def entity_resolution(state):
        calls.append("entity")
        return entity

    async def profile_intake(state):
        calls.append("intake")
        return intake

    async def technical_price(state):
        calls.append("price")
        return {"price_records": [{"record_id": "x", "field": "current_price", "value": 2950.0,
                                   "source": "yfinance", "tier": 2, "as_of": "2026-10-03"}]}

    async def fundamentals(state):
        calls.append("fundamentals")
        return {"fundamentals_records": [{"record_id": "y", "field": "revenue", "value": 1e12,
                                          "source": "trendlyne", "tier": 2, "as_of": "2026", "fiscal_year": 2026}]}

    async def macro(state):
        calls.append("macro")
        return {"macro_records": [], "macro_panel": {"countries": {}, "flags": []}}

    async def news(state):
        calls.append("news")
        return {"news_records": [], "news": {"items": [], "filings": [], "flags": [], "summary": {}}}

    async def geo_sector(state):
        calls.append("geo")
        return {"geo_sector_records": [], "geo_exposure": {"weights": {"IN": 1.0}, "source": "home_default",
                                                          "reliable": False, "flags": []}}

    return {"entity_resolution": entity_resolution, "profile_intake": profile_intake,
            "technical_price": technical_price, "fundamentals": fundamentals, "macro": macro, "news": news, "geo_sector": geo_sector}


RESOLVED = {"entity_status": "resolved", "ticker": "RELIANCE", "exchange": "NSE", "market": "IN",
            "listing_currency": "INR", "company_name": "Reliance Industries Limited"}


def run(entity, intake):
    calls = []
    graph = build_graph(fakes(entity, intake, calls))
    return asyncio.run(graph.ainvoke({"raw_input": "x"})), calls


def test_full_run_reaches_price_fundamentals_and_preview():
    result, calls = run(RESOLVED, {"user_profile": dict(PROFILE)})
    assert set(calls) == {"entity", "intake", "price", "fundamentals", "macro", "news", "geo"}     # all retrieval agents ran
    assert "Fundamentals FY2026" in result["preview_response"]
    assert "Reliance Industries Limited" in result["final_response"]
    assert result["final_response"].startswith("Information only, not investment advice.")
    assert result["user_profile"]["budget_currency"] == "INR"        # currency default from the listing
    assert result["user_profile"]["budget_currency_assumed"] is True


def test_entity_question_stops_before_retrieval():
    result, calls = run({"entity_status": "ask_user", "entity_question": "Did you mean A or B?"},
                        {"user_profile": dict(PROFILE)})
    assert "price" not in calls and "fundamentals" not in calls and result["final_response"] == "Did you mean A or B?"


def test_both_questions_are_asked_together_with_the_company_name():
    result, calls = run(RESOLVED, {"user_profile": {}, "missing_profile_fields": ["budget", "goal"],
                                   "followup_question": "old text", "followup_asked": True})
    assert "price" not in calls
    assert result["final_response"].startswith("Before I analyse Reliance Industries Limited")


def test_not_covered_ends_with_the_reason():
    result, calls = run({**RESOLVED, "entity_status": "not_covered", "not_covered_reason": "it is a REIT"},
                        {"user_profile": dict(PROFILE)})
    assert "price" not in calls and result["verdict"] == "not_covered"
    assert "Reliance Industries Limited is not covered in version 1 because it is a REIT." in result["final_response"]
    assert result["final_response"].startswith("Information only") and "Disclaimer: " in result["final_response"]


def test_us_stock_with_a_rupee_budget_goes_through_fx():
    from invest_agent.fx.step import make_fx_node

    async def rate(base, quote):
        return {"base": base, "quote": quote, "rate": 96.2, "date": "2026-10-05", "source": "frankfurter",
                "provider": "official blend", "tier": 1, "flags": [],
                "cross_check": {"status": "agree", "date": "2026-09-25", "fred_rate": 95.81,
                                "frankfurter_rate": 95.8, "gap": 0.0001}}

    async def series(base, quote, months):
        return {"points": [{"date": "2025-10-01", "rate": 88.34}], "flags": []}

    apple = {**RESOLVED, "ticker": "AAPL", "exchange": "Nasdaq", "market": "US", "listing_currency": "USD",
             "company_name": "Apple Inc."}
    calls = []
    nodes = fakes(apple, {"user_profile": {**PROFILE, "budget_currency": "INR"}}, calls)
    nodes["currency_normalisation"] = make_fx_node(rate, series)
    result = asyncio.run(build_graph(nodes).ainvoke({"raw_input": "x"}))
    text = result["preview_response"]
    assert "Budget: ₹2,00,000 (about $2,079.00 in USD, indicative)" in text
    assert "1 USD = 96.20 INR (Frankfurter, official blend, 2026-10-05)" in text
    assert "dollar rose 8.9% against the rupee" in text
    assert result["fx_records"][0]["value"] == 96.2
