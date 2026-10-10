"""
tests/unit/test_grounding.py: the grounding guardrail (design 14.12), with made-up facts.

What this file checks:
    an untagged number, an invented number, a fact that does not exist: each fails
    rounding as shown passes
    a failing sentence is removed whole; a paragraph losing more than half is dropped
    any removal lowers confidence once
    a verdict change whose reason fails is reversed
    more than three removals bring in the templated reasoning
    the meaning check removes a "not supported" sentence and is skipped for templated text
    a meaning check that fails leaves Check 1 standing, flagged
"""

from __future__ import annotations

import asyncio

from invest_agent.guardrails.grounding import check_sentence, ground
from invest_agent.guardrails.meaning_check import Verdict, Verdicts

FACTS = [
    {"id": "F1", "block": "scorecard", "text": "Weighted score -0.31; preliminary verdict neutral", "record_ids": [],
     "kind": "scorecard"},
    {"id": "F2", "block": "business", "text": "ROIC averaged 12.43% over the last 3 years", "record_ids": ["a:X:roic"],
     "kind": "computed"},
    {"id": "F3", "block": "valuation", "text": "P/E 21.5 against a peer median of 9.6 (5 peers), 123% dearer",
     "record_ids": ["computed:X:peer_median_pe"], "kind": "computed"},
    {"id": "F4", "block": "business", "text": "FY2026: revenue ₹10,85,997 crore", "record_ids": ["t:X:revenue:FY2026"],
     "kind": "reported"},
]
BY_ID = {f["id"]: f for f in FACTS}


async def none_supported(system, body):
    return Verdicts(items=[])


def run(syn, ask=none_supported, preliminary="neutral", confidence="medium"):
    return asyncio.run(ground(syn, FACTS, preliminary=preliminary, preliminary_confidence=confidence, ask=ask))


def syn(reasoning, **kw):
    return {"verdict": kw.pop("verdict", "neutral"), "confidence": "medium", "reasoning": reasoning,
            "key_risks": kw.pop("key_risks", []), "what_would_change_the_view": [], "templated": False, **kw}


# --- Check 1 ------------------------------------------------------------------------------------

def test_sentence_rules():
    assert check_sentence("ROIC averaged 12.4% [F2].", BY_ID)["ok"]
    assert check_sentence("Revenue was ₹10,85,997 crore [F4].", BY_ID)["ok"]
    assert not check_sentence("ROIC averaged 12.4%.", BY_ID)["ok"]                       # no tag
    assert not check_sentence("ROIC averaged 14.0% [F2].", BY_ID)["ok"]                  # invented
    assert not check_sentence("P/E 21.5 [F9].", BY_ID)["ok"]                             # no such fact
    assert not check_sentence("ROIC averaged 12.4% [F3].", BY_ID)["ok"]                  # wrong fact
    assert check_sentence("The business is sound.", BY_ID)["ok"]                         # no number


# --- Failure handling ---------------------------------------------------------------------------

def test_failing_sentence_removed_and_confidence_lowered():
    out = run(syn(["ROIC averaged 12.4% [F2]. Revenue grew 30% [F4]. The business is sound."]))
    assert out["reasoning"][0]["text"] == "ROIC averaged 12.4% [F2]. The business is sound."
    assert out["reasoning"][0]["record_ids"] == ["a:X:roic"]
    assert out["removed_sentences"] == ["Revenue grew 30% [F4]."]
    assert out["confidence"] == "low"


def test_paragraph_losing_more_than_half_is_dropped():
    out = run(syn(["Revenue grew 30% [F4]. Margins hit 50% [F2]. The business is sound.", "P/E is 21.5 [F3]."]))
    assert [p["text"] for p in out["reasoning"]] == ["P/E is 21.5 [F3]."]


def test_failed_change_reason_reverses_the_verdict():
    s = syn(["P/E is 21.5 [F3]."], verdict="unfavourable", changed=True,
            change_reason="The P/E is 45.0, far above peers.", change_sources=["F3"])
    out = run(s)
    assert out["verdict"] == "neutral" and "verdict_change_reversed" in out["guardrail_flags"]


def test_good_change_reason_stands():
    s = syn(["P/E is 21.5 [F3]."], verdict="unfavourable", changed=True,
            change_reason="The P/E of 21.5 is 123% above peers.", change_sources=["F3"])
    out = run(s)
    assert out["verdict"] == "unfavourable" and out["verdict_change_reason"]


def test_more_than_three_removals_bring_in_the_template():
    bad = " ".join(f"Growth was {n}% [F2]." for n in (31, 32, 33, 34))
    out = run(syn([bad + " Fine.", "P/E is 21.5 [F3]."]))
    assert "reasoning_replaced_by_template" in out["guardrail_flags"]
    assert any("[F2]" in p["text"] for p in out["reasoning"])


def test_risk_with_a_bad_number_is_dropped():
    out = run(syn(["P/E is 21.5 [F3]."], key_risks=["The P/E of 99 is extreme [F3].", "Dear against peers [F3]."]))
    assert [r["text"] for r in out["key_risks"]] == ["Dear against peers [F3]."]


# --- Meaning check ------------------------------------------------------------------------------

def test_meaning_check_removes_wrong_meaning():
    async def ask(system, body):
        assert "sentence: Profit was ₹10,85,997 crore [F4]." in body
        return Verdicts(items=[Verdict(id="s2", result="not_supported", reason="revenue, not profit")])

    out = run(syn(["P/E is 21.5 [F3]. Profit was ₹10,85,997 crore [F4]."]), ask=ask)
    assert out["reasoning"][0]["text"] == "P/E is 21.5 [F3]."


def test_meaning_check_failure_is_flagged_not_fatal():
    async def ask(system, body):
        raise TimeoutError

    out = run(syn(["P/E is 21.5 [F3]."]), ask=ask)
    assert out["reasoning"] and any(f.startswith("meaning_check_failed") for f in out["guardrail_flags"])


def test_templated_text_skips_the_meaning_check():
    async def ask(system, body):
        raise AssertionError("must not be called")

    out = run(syn(["P/E 21.5 against a peer median of 9.6 (5 peers), 123% dearer [F3]."], templated=True), ask=ask)
    assert out["removed_sentences"] == []
