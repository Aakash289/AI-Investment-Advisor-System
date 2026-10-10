"""
Unit tests for the Entity Resolution node (agents/entity_resolution.py).

The model call and the MCP call are replaced by small fake functions, so these tests
check the node's own logic (what it sends, what it writes into the state) without an
API key, a network connection or a running server. Run with:
    uv run pytest tests/unit/test_entity_node.py -v
"""

import asyncio

from invest_agent.agents.entity_resolution import CompanyReference, make_entity_resolution_node

AAPL = {"ticker": "AAPL", "name": "Apple Inc.", "exchange": "Nasdaq", "market": "US",
        "currency": "USD", "cik": 320193, "isin": None, "yahoo_symbol": "AAPL"}
INFY_NSE = {"ticker": "INFY", "name": "Infosys Limited", "exchange": "NSE", "market": "IN",
            "currency": "INR", "cik": None, "isin": "INE009A01021", "yahoo_symbol": "INFY.NS"}


def fake_extract(answer: CompanyReference):
    async def extract(message, options):
        return answer
    return extract


class FakeResolve:
    """Records what the node asked for and returns a canned tool answer."""

    def __init__(self, answer: dict):
        self.answer, self.calls, self.guesses = answer, [], []

    async def __call__(self, query, confirmed, guess=None):
        self.calls.append((query, confirmed))
        self.guesses.append(guess)
        return self.answer


def run(node, state):
    return asyncio.run(node(state))


def test_resolved_company_fills_the_state():
    resolve = FakeResolve({"status": "resolved", "listing": AAPL, "candidates": [], "question": None,
                           "reason": None, "flags": [], "sec_profile": {"sic": 3571}})
    node = make_entity_resolution_node(fake_extract(CompanyReference(company_reference="apple")), resolve)
    out = run(node, {"raw_input": "should I invest in apple"})
    assert resolve.calls == [("apple", False)]
    assert out["entity_status"] == "resolved" and out["ticker"] == "AAPL"
    assert out["listing_currency"] == "USD" and out["sic_code"] == 3571 and out["cik"] == "320193"


def test_ticker_is_sent_in_capitals():
    resolve = FakeResolve({"status": "resolved", "listing": AAPL, "candidates": []})
    node = make_entity_resolution_node(
        fake_extract(CompanyReference(company_reference="aapl", looks_like_ticker=True)), resolve)
    run(node, {"raw_input": "aapl?"})
    assert resolve.calls == [("AAPL", False)]


def test_choice_after_a_question_is_resolved_exactly_and_confirmed():
    resolve = FakeResolve({"status": "resolved", "listing": INFY_NSE, "candidates": []})
    node = make_entity_resolution_node(fake_extract(CompanyReference(chosen_option=1)), resolve)
    state = {"raw_input": "the NSE one", "entity_status": "ask_user", "entity_candidates": [INFY_NSE, AAPL]}
    out = run(node, state)
    assert resolve.calls == [("NSE:INFY", True)]
    assert out["market"] == "IN" and out["entity_candidates"] == []


def test_no_company_in_the_message_asks():
    resolve = FakeResolve({})
    node = make_entity_resolution_node(fake_extract(CompanyReference()), resolve)
    out = run(node, {"raw_input": "what should I buy?"})
    assert out["entity_status"] == "not_found" and resolve.calls == []


def test_not_covered_carries_the_reason():
    resolve = FakeResolve({"status": "not_covered", "listing": AAPL, "candidates": [],
                           "reason": "it is a REIT", "flags": []})
    node = make_entity_resolution_node(fake_extract(CompanyReference(company_reference="x")), resolve)
    out = run(node, {"raw_input": "x"})
    assert out["entity_status"] == "not_covered" and out["not_covered_reason"] == "it is a REIT"


def test_reply_without_a_company_keeps_the_resolved_one():
    resolve = FakeResolve({})
    node = make_entity_resolution_node(fake_extract(CompanyReference()), resolve)
    out = run(node, {"raw_input": "2 lakhs, low risk", "entity_status": "resolved", "ticker": "AAPL"})
    assert out == {} and resolve.calls == []


def test_the_model_guess_is_passed_for_names_but_not_tickers():
    resolve = FakeResolve({"status": "resolved", "listing": AAPL, "candidates": []})
    node = make_entity_resolution_node(
        fake_extract(CompanyReference(company_reference="reliance", full_name_guess="Reliance Industries Limited")),
        resolve)
    run(node, {"raw_input": "reliance"})
    assert resolve.guesses == ["Reliance Industries Limited"]
    resolve2 = FakeResolve({"status": "resolved", "listing": AAPL, "candidates": []})
    node2 = make_entity_resolution_node(
        fake_extract(CompanyReference(company_reference="RS", looks_like_ticker=True, full_name_guess="Reliance, Inc.")),
        resolve2)
    run(node2, {"raw_input": "RS"})
    assert resolve2.guesses == [None]