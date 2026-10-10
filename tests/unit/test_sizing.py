"""
tests/unit/test_sizing.py: position sizing and the concentration check (design 14.13).

What this file checks (version 2, 6 October 2026: a share of a full position):
    a full position is the concentration limit; the share of it comes from where the
    weighted score sits in the verdict's band, then confidence
    Reliance-like neutral at low confidence for a low-risk ₹2,00,000 budget: 5 shares
    neutral and favourable notes say what a full position is and what is left
    unfavourable gives zero, explained
    withheld: insufficient data, verification withholding, no budget
    the person's own limit sets the full position
    one share costing more than the amount, and more than a full position
    a rupee budget for a dollar stock, converted and back
    missing volatility: no scaling, flagged
    the liquidity warning; the 5-year growth beside the market index
"""

from __future__ import annotations

import pytest

from invest_agent.common.records import make_record
from invest_agent.decision.sizing import HOLDING_NOTE, size


def rec(field, value):
    return make_record(source="test", tier=2, entity="X", field=field, value=value, as_of="2026-10-06")


def state(verdict="favourable", confidence="medium", risk="medium", budget=200000, price=2400.0, vol=0.40,
          daily=5e9, limit=None, currency="INR", fx=None, score=None, **extra):
    records = [rec("current_price", price), rec("avg_traded_value_3m", daily)]
    if vol is not None:
        records.append(rec("volatility_1y", vol))
    profile = {"budget_amount": budget, "budget_currency": "INR", "risk_tolerance": risk}
    if limit:
        profile["concentration_limit_pct"] = limit
    card = {"score": score} if score is not None else {}
    return {"verdict": verdict, "confidence": confidence, "user_profile": profile, "verified_records": records,
            "listing_currency": currency, "fx": fx or {"needed": False}, "scorecard": card, **extra}


def test_full_position_and_share_of_it():
    # Favourable +1.125 (halfway up 0.75 to 1.5): 80% of a full position, medium confidence x 0.9 = 72%.
    # Medium risk: full position 15% of ₹2,00,000 = ₹30,000; 72% = ₹21,600; x 30/40 = ₹16,200: 6 shares.
    s = size(state(score=1.125))
    assert s["full_position"] == pytest.approx(30000) and s["shares"] == 6
    assert s["amount"] == pytest.approx(6 * 2400)
    assert [st["step"] for st in s["steps"]] == [1, 2, 3, 4, 5]
    assert "= 72% = ₹21,600" in s["steps"][1]["text"] and "x 0.75" in s["steps"][2]["text"]
    assert HOLDING_NOTE in s["notes"] and "A full position for your profile is ₹30,000" in s["notes"][0]


def test_unfavourable_gives_zero():
    s = size(state(verdict="unfavourable", confidence="high"))
    assert s["status"] == "zero" and s["amount"] == 0 and "argues against" in s["notes"][0]


def test_reliance_like_neutral():
    # Neutral -0.03 at low confidence, low risk, 20.9% volatility, ₹1,218 a share.
    # Full position ₹20,000; the neutral band runs -0.5 to +0.75 (scorecard version 5), so -0.03
    # sits 37.6% up it: 30% + 30% x 0.376 = 41.3%, x 0.8 = 33.0% = ₹6,605; x 20/20.9 = ₹6,320:
    # 5 shares, ₹6,090.
    s = size(state(verdict="neutral", confidence="low", risk="low", vol=0.209, price=1218.0, score=-0.03))
    assert s["shares"] == 5 and s["amount"] == pytest.approx(5 * 1218.0)
    assert s["share_of_full"] == pytest.approx(6320 / 20000, abs=0.002)
    assert "full position for your profile is ₹20,000" in s["notes"][0] and "neutral" in s["notes"][0]


def test_score_moves_the_amount_within_the_band():
    low = size(state(verdict="neutral", confidence="high", vol=0.2, score=-0.75))
    high = size(state(verdict="neutral", confidence="high", vol=0.2, score=0.75))
    assert low["suggested"] == pytest.approx(0.30 * 30000) and high["suggested"] == pytest.approx(0.60 * 30000)


@pytest.mark.parametrize("extra,why", [
    ({"verdict": "insufficient_data"}, "not complete enough for a verdict"),
    ({"amount_withheld": True, "amount_withheld_reason": "critical data missing: shares"}, "critical data missing"),
    ({"budget": None}, "no budget was provided"),
])
def test_withheld(extra, why):
    s = size(state(**extra))
    assert s["status"] == "withheld" and why in s["reason"] and s["amount"] is None


def test_never_more_than_a_full_position():
    s = size(state(confidence="high", risk="low", vol=0.15, score=2.0))       # top of the band, high confidence
    assert s["suggested"] == pytest.approx(20000) and s["overexposure_note"] is None
    assert "default limit for your risk tolerance" in " ".join(s["notes"])


def test_own_limit_wins():
    s = size(state(confidence="high", risk="high", vol=0.30, limit=5, score=1.5))
    assert s["full_position"] == pytest.approx(10000) and "your own limit" in s["steps"][0]["text"]


def test_share_costs_more_than_the_amount():
    s = size(state(confidence="low", price=50000.0))
    assert s["shares"] == 0 and any("One share costs" in n for n in s["notes"])


def test_rupee_budget_for_a_dollar_stock():
    fx = {"needed": True, "rate": {"rate": 96.2, "base": "USD", "quote": "INR"}}
    s = size(state(verdict="neutral", confidence="low", risk="low", vol=0.246, price=332.89, currency="USD", fx=fx,
                   score=0.0))
    # Full ₹20,000; 0 sits 40% up the band (-0.5 to +0.75): 42% x 0.8 = 33.6% = ₹6,720;
    # x 20/24.6 = ₹5,463 = $56.79, less than one share,
    # and one share (about ₹32,024) costs more than the whole full position.
    assert s["shares"] == 0 and [st["step"] for st in s["steps"]] == [1, 2, 3, 4, 5, 6]
    assert s["steps"][3]["name"] == "currency" and "$56.79" in s["steps"][3]["text"]
    note = next(n for n in s["notes"] if "One share costs" in n)
    assert "more than a full position" in note and "from India sell part of a share" in note


def test_dollar_stock_buys_whole_shares():
    fx = {"needed": True, "rate": {"rate": 96.2, "base": "USD", "quote": "INR"}}
    s = size(state(budget=5000000, confidence="high", risk="low", vol=0.20, price=332.89, currency="USD", fx=fx,
                   score=1.5))
    # Full position 10% of ₹50,00,000 = ₹5,00,000 = $5,197.51: 15 shares.
    assert s["shares"] == 15 and s["amount"] == pytest.approx(15 * 332.89 * 96.2)


def test_missing_volatility_is_flagged():
    s = size(state(vol=None))
    assert "no_volatility" in s["flags"] and "no scaling" in s["steps"][2]["text"]


def test_liquidity_warning():
    s = size(state(daily=500000.0))
    assert s["liquidity_note"] and "average daily trading" in s["liquidity_note"]


def test_five_year_growth_beside_the_index():
    st = state(verdict="neutral", confidence="low", score=0.0, price_chart={"benchmark_symbol": "^NSEI"})
    st["verified_records"] += [rec("price_cagr_5y", 0.010), rec("benchmark_cagr_5y", 0.118)]
    note = next(n for n in size(st)["notes"] if "Over the last 5 years" in n)
    assert "+1.0% a year, against +11.8% a year for the Nifty 50" in note
