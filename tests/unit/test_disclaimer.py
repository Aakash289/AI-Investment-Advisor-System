"""
tests/unit/test_disclaimer.py: compliance, the disclaimer and the user's answer (design 14.15).

What this file checks:
    banned phrases    "guaranteed", "risk-free" (with a space or hyphen), "can't lose"
                      (straight or curly apostrophe) remove the sentence; a quoted headline
                      is kept; "guarantee" inside another word is not matched
    situation lines   each one appears only when it applies
    the answer        short line first, full disclaimer last, fact tags taken out, the
                      fair value labelled a projection (and low reliability when the peers
                      are weak), the existing-holding line with any amount
    hard rule         a missing disclaimer gives the refusal and nothing else
    not covered       the reason, with the disclaimer
"""

from __future__ import annotations

import pytest

from invest_agent.compliance import disclaimer
from invest_agent.compliance.disclaimer import REFUSAL, compliance_node, situation_lines, untag
from invest_agent.compliance.phrase_check import banned_in, clean

SIZING_OK = {"status": "ok", "currency": "INR", "stock_currency": "INR", "amount": 3647.0, "amount_stock": 3647.0,
             "shares": 3, "price": 1215.7, "suggested": 3824.0, "suggested_stock": 3824.0,
             "notes": ["This amount assumes you do not already own this stock."]}


def base(**extra):
    return {"company_name": "Reliance Industries Limited", "ticker": "RELIANCE", "exchange": "NSE",
            "listing_currency": "INR", "verdict": "neutral", "confidence": "low",
            "user_profile": {"budget_amount": 200000, "budget_currency": "INR", "risk_tolerance": "low",
                             "horizon_years": 10, "goal": "growth"},
            "sizing": SIZING_OK, "fair_value": {"status": "ok", "range": {"low": 450, "base": 536, "high": 653}},
            "reasoning": [{"text": "Revenue has grown about 15.5% a year on trend [F9]."}],
            "key_risks": [{"text": "The fair value range is below the price [F18]."}],
            "what_would_change_the_view": ["A broader peer set."], **extra}


# --- Banned phrases -----------------------------------------------------------------------------

@pytest.mark.parametrize("sentence,found", [
    ("Returns are guaranteed.", "guaranteed"),
    ("It is risk-free.", "risk-free"),
    ("It is risk free.", "risk free"),
    ("You can’t lose here.", "can’t lose"),
    ("You can't lose here.", "can't lose"),
    ("Buy now while it is cheap.", "Buy now"),
    ('The outlet ran "Buy now before it doubles" as a headline.', None),
    ("The company gave no guarantees about margins.", None),
])
def test_banned_phrases(sentence, found):
    assert banned_in(sentence) == found


def test_clean_removes_whole_sentences_and_logs():
    out = clean({"reasoning": [{"text": "Growth is steady [F9]. Returns are guaranteed [F10]."}],
                 "key_risks": [{"text": "It is risk-free."}], "what_would_change_the_view": ["Nothing."]})
    assert out["reasoning"][0]["text"] == "Growth is steady [F9]."
    assert out["key_risks"] == [] and len(out["removed"]) == 2


# --- Situation lines ----------------------------------------------------------------------------

def test_situation_lines():
    assert situation_lines(base()) == ["This amount assumes you do not already own this stock."]
    assert situation_lines(base(sizing={"status": "withheld", "reason": "no budget was provided"})) == \
        ["No amount is given because no budget was provided."]
    withheld = situation_lines(base(sizing={"status": "withheld", "reason": "critical data missing: shares"}))
    assert withheld == ["No amount is given because critical data missing: shares."]
    insufficient = situation_lines(base(verdict="insufficient_data",
                                        sizing={"status": "withheld",
                                                "reason": "the data was not complete enough for a verdict"}))
    assert insufficient == ["The data was not complete enough for a verdict."]
    assert "Some data was skipped" in situation_lines(base(partial_analysis=True))[0]
    assert situation_lines(base(sizing={"status": "zero"})) == []


# --- The answer ---------------------------------------------------------------------------------

def test_answer_shape():
    text = compliance_node(base(scorecard={"low_reliability": ["valuation", "forecast"]}))["final_response"]
    cfg = disclaimer.config()
    assert text.startswith(cfg["short"]) and text.rstrip().endswith(cfg["full"].strip())
    assert "Verdict: Neutral, confidence low" in text
    assert "about ₹3,647, which buys 3 shares" in text
    assert "[F9]" not in text and "about 15.5% a year on trend." in text
    assert "(a projection, not a reported figure): ₹450 to ₹653" in text and "low reliability" in text
    assert text.count("assumes you do not already own this stock") == 1


def test_less_than_one_share():
    sizing = {**SIZING_OK, "currency": "INR", "stock_currency": "USD", "shares": 0, "price": 332.89,
              "suggested": 3247.0, "suggested_stock": 33.75}
    text = compliance_node(base(sizing=sizing, listing_currency="USD"))["final_response"]
    assert "about ₹3,247 (about $33.75), less than the price of one share ($332.89)" in text


def test_untag():
    assert untag("Growth was 8.4% [F5, F9]. Margins [F2], too.") == "Growth was 8.4%. Margins, too."


# --- Hard rule and not covered ------------------------------------------------------------------

def test_no_disclaimer_no_output(monkeypatch):
    monkeypatch.setattr(disclaimer, "config", lambda: {"version": "x", "short": "", "full": "", "situations": {}})
    out = compliance_node(base())
    assert out["final_response"] == REFUSAL and out["disclaimer_version"] is None


def test_not_covered_carries_the_disclaimer():
    out = compliance_node({"verdict": "not_covered", "company_name": "Tiny Co",
                           "not_covered_reason": "its market value is below ₹500 crore"})
    text = out["final_response"]
    assert "Tiny Co is not covered in version 1 because its market value is below ₹500 crore." in text
    assert text.startswith("Information only") and "Disclaimer: " in text and out["disclaimer_version"] == "1"
