"""
tests/unit/test_synthesis_validate.py: the Synthesis node and its verdict rules (design 14.11).

What this file checks:
    verdict rules   keep is fine; one step with a reason and cited facts is accepted; two
                    steps, a capped verdict, insufficient data, no reason, or invented fact
                    ids are rejected and the scorecard verdict kept
    the node        an unreadable answer is retried once with the error; two failures give
                    the templated reasoning; insufficient data makes no model call
    the facts       built in the fixed block order, with a weak-peer note when the company
                    is more than 100% dearer than its peers (A1)
"""

from __future__ import annotations

import asyncio

import pytest

from invest_agent.agents.synthesis import make_synthesis_node
from invest_agent.agents.synthesis_facts import BLOCKS, build_facts, weak_peer_note
from invest_agent.agents.synthesis_validate import SynthesisOutput, check_verdict, templated

IDS = {"F1", "F2", "F3"}


def out(verdict="neutral", changed=False, reason=None, sources=()):
    return SynthesisOutput(verdict=verdict, changed_from_scorecard=changed, change_reason=reason,
                           change_sources=list(sources), reasoning=["Text."])


# --- Verdict rules ------------------------------------------------------------------------------

def test_keeping_the_verdict():
    assert check_verdict(out(), "neutral", capped=False, fact_ids=IDS)["verdict"] == "neutral"


def test_one_step_with_reason_and_facts():
    d = check_verdict(out("unfavourable", True, "Peers look weak.", ["F2"]), "neutral", capped=False, fact_ids=IDS)
    assert d["accepted"] and d["changed"] and d["verdict"] == "unfavourable"


@pytest.mark.parametrize("o,prelim,capped,why", [
    (out("unfavourable", True, "x", ["F2"]), "favourable", False, "more than one step"),
    (out("favourable", True, "x", ["F2"]), "neutral", True, "capped"),
    (out("neutral", True, "x", ["F2"]), "insufficient_data", False, "insufficient"),
    (out("favourable", True, None, ["F2"]), "neutral", False, "without a reason"),
    (out("favourable", True, "x", ["F99"]), "neutral", False, "without a cited fact"),
])
def test_rejected_changes(o, prelim, capped, why):
    d = check_verdict(o, prelim, capped=capped, fact_ids=IDS)
    assert d["verdict"] == prelim and not d["accepted"] and why in d["rejected_because"]


# --- The node -----------------------------------------------------------------------------------

STATE = {"company_name": "X Corp", "ticker": "X", "exchange": "NYSE", "listing_currency": "USD",
         "preliminary_verdict": "neutral", "preliminary_confidence": "medium",
         "scorecard": {"status": "ok", "score": -0.31, "verdict": "neutral", "raw_verdict": "neutral", "caps": [],
                       "capped": False, "confidence": {"level": "medium"}},
         "sub_scores": {"business_quality": 0.5, "valuation": -2.0},
         "findings": [{"component": "valuation_pe", "score": -2, "text": "P/E 21.5 against a peer median of 9.6",
                       "record_ids": ["computed:X:peer_median_pe"]}],
         "user_profile": {"budget_amount": 200000, "budget_currency": "INR", "risk_tolerance": "low",
                          "horizon_years": 10, "goal": "growth"}}


def test_unreadable_answer_is_retried_once():
    bodies = []

    async def call(system, body):
        bodies.append(body)
        if len(bodies) == 1:
            raise ValueError("bad json")
        return out()

    res = asyncio.run(make_synthesis_node(call)(STATE))
    assert len(bodies) == 2 and "could not be read (ValueError: bad json)" in bodies[1]
    assert res["synthesis"]["verdict"] == "neutral" and not res["synthesis"]["templated"]


def test_two_failures_give_the_template():
    async def call(system, body):
        raise ValueError("bad json")

    res = asyncio.run(make_synthesis_node(call)(STATE))
    assert res["synthesis"]["templated"] and "synthesis_failed_templated" in res["synthesis"]["flags"]
    assert any("[F" in p for p in res["synthesis"]["reasoning"])


def test_insufficient_data_makes_no_call():
    async def call(system, body):
        raise AssertionError("must not be called")

    state = {**STATE, "preliminary_verdict": "insufficient_data",
             "scorecard": {"status": "insufficient_data", "reasons": ["there is no current share price"]}}
    res = asyncio.run(make_synthesis_node(call)(state))
    assert res["synthesis"]["verdict"] == "insufficient_data"


def test_rejected_change_is_flagged():
    async def call(system, body):
        return out("favourable", True, "Because.", ["F1"])

    res = asyncio.run(make_synthesis_node(call)({**STATE, "preliminary_verdict": "unfavourable"}))
    assert res["synthesis"]["verdict"] == "unfavourable"
    assert any("more than one step" in f for f in res["synthesis"]["flags"])


# --- The facts ----------------------------------------------------------------------------------

def test_facts_follow_the_block_order():
    facts = build_facts(STATE)
    order = [BLOCKS.index(f["block"]) for f in facts]
    assert order == sorted(order) and facts[0]["block"] == "profile"
    assert [f["id"] for f in facts] == [f"F{i}" for i in range(1, len(facts) + 1)]


def test_weak_peer_note():
    val = {"peers": {"comparisons": {"pe": {"premium": 1.23}, "ev_ebitda": {"premium": 1.89}}}}
    note = weak_peer_note(val)
    assert "P/E 123% above" in note and "EV/EBITDA 189% above" in note
    assert weak_peer_note({"peers": {"comparisons": {"pe": {"premium": 0.38}}}}) is None


def test_template_cites_every_fact_it_uses():
    facts = build_facts(STATE)
    t = templated(facts, "neutral", "medium")
    assert t["templated"] and all("[F" in p for p in t["reasoning"])
