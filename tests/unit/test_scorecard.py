"""
tests/unit/test_scorecard.py: the scorecard, overrides, suitability and confidence (design 14.11).

What this file checks:
    scorecard     weights 50/20/15/5/10 (version 2), a missing forecast's weight shared
                  out, a low-reliability score at half weight, the
                  +-0.75 thresholds, insufficient data (no price, too few years)
    overrides     a restatement 8-K and a news item marked as a threat cap a strong
                  company at neutral; routine news does not
    suitability   capital preservation with a volatile stock, income with no dividend,
                  a short horizon, liquidity needs with a thinly traded stock, net cash
                  passing the debt test, banks skipping it
    confidence    agreement gives high, strong signals both ways give low, partial data
                  lowers it one step
    the decision  Apple-like and Reliance-like sub-scores end where the live runs did

No network: every input is built in the test.
"""

from __future__ import annotations

import pytest

from invest_agent.analysis import confidence, scorecard, suitability
from invest_agent.analysis.step import decide
from invest_agent.common.records import make_record

YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def rec(field, value, year=None):
    extra = {"fiscal_year": year, "period": f"FY{year}"} if year else {}
    return make_record(source="test", tier=1, entity="X", field=field, value=value, as_of="2026-03-31", **extra)


def records(years=YEARS, price=100.0, vol=0.25, net_debt=50.0, nde=1.0, dividends=-5.0, thin=False):
    rows = [rec("revenue", 100, y) for y in years] + [rec("net_income", 10, y) for y in years]
    if price is not None:
        rows.append(rec("current_price", price))
    rows += [rec("volatility_1y", vol), rec("thinly_traded", thin), rec("net_debt", net_debt, years[-1]),
             rec("net_debt_to_ebitda", nde, years[-1]), rec("dividends_paid", dividends, years[-1])]
    return rows


GOOD = {"business_quality": 1.5, "valuation": 1.0, "macro_fit": 0.5, "news": 0.5, "forecast": 1.0}


# --- Scorecard ----------------------------------------------------------------------------------

def test_weights_and_thresholds():
    w = scorecard.weighted(GOOD)
    assert w["score"] == pytest.approx(0.5 * 1.5 + 0.2 * 1.0 + 0.15 * 0.5 + 0.05 * 0.5 + 0.1 * 1.0)
    assert scorecard.verdict_from(0.75) == "favourable"
    assert scorecard.verdict_from(0.74) == "neutral"
    assert scorecard.verdict_from(-0.75) == "unfavourable"


def test_missing_forecast_weight_is_shared_out():
    w = scorecard.weighted({**GOOD, "forecast": None})
    assert w["missing"] == ["forecast"]
    assert w["weights_used"]["business_quality"] == pytest.approx(50 / 90, abs=1e-4)
    assert w["score"] == pytest.approx((50 * 1.5 + 20 * 1.0 + 15 * 0.5 + 5 * 0.5) / 90, abs=1e-4)


def test_low_reliability_counts_at_half_weight():
    w = scorecard.weighted({**GOOD, "forecast": None}, low_reliability=["valuation"])
    assert w["weights_used"]["valuation"] == pytest.approx(10 / 80, abs=1e-4)


def test_insufficient_data():
    assert scorecard.insufficient(records()) == []
    assert "there is no current share price" in scorecard.insufficient(records(price=None))
    assert scorecard.insufficient(records(years=[2025, 2026]))
    out = decide({"verified_records": records(years=[2025, 2026])}, GOOD)
    assert out["preliminary_verdict"] == "insufficient_data"


# --- Overrides ----------------------------------------------------------------------------------

def test_restatement_caps_a_strong_company():
    news = {"ticker": "X", "filings": [{"date": "2026-09-01", "title": "4.02 non-reliance", "items": ["4.02"],
                                         "accession": "0001", "serious": True}]}
    out = decide({"verified_records": records(), "news": news}, GOOD)
    assert out["scorecard"]["raw_verdict"] == "favourable"
    assert out["preliminary_verdict"] == "neutral" and out["scorecard"]["capped"]
    assert out["scorecard"]["caps"][0]["record_ids"] == ["sec_edgar:X:8k_filing:0001"]


def test_news_threat_caps_but_routine_news_does_not():
    item = {"id": "n1", "title": "Licence cancelled", "origin": "tavily", "date": "2026-10-01", "outlet": "reuters.com"}
    threat = {"ticker": "X", "items": [item], "summary": {"serious_negative_ids": ["n1"]}}
    routine = {"ticker": "X", "items": [item], "summary": {"serious_negative_ids": []}}
    assert decide({"verified_records": records(), "news": threat}, GOOD)["preliminary_verdict"] == "neutral"
    assert decide({"verified_records": records(), "news": routine}, GOOD)["preliminary_verdict"] == "favourable"


def test_cap_never_raises_a_verdict():
    assert scorecard.cap("unfavourable") == "unfavourable"
    assert scorecard.cap("favourable") == "neutral"


# --- Suitability --------------------------------------------------------------------------------

def test_capital_preservation_with_a_volatile_stock():
    out = suitability.check({"goal": "capital_preservation", "horizon_years": 10}, records(vol=0.42), bank_mode=False)
    assert out["caps"] and "42%" in out["caps"][0]["text"]


def test_high_debt_but_net_cash_and_banks_pass():
    profile = {"goal": "capital_preservation", "horizon_years": 10}
    assert suitability.check(profile, records(nde=4.0), bank_mode=False)["caps"]
    assert not suitability.check(profile, records(nde=4.0, net_debt=-10.0), bank_mode=False)["caps"]
    assert not suitability.check(profile, records(nde=4.0), bank_mode=True)["caps"]


def test_income_goal_without_a_dividend():
    out = suitability.check({"goal": "income", "horizon_years": 10}, records(dividends=0.0), bank_mode=False)
    assert out["caps"][0]["rule"] == "income"
    assert not suitability.check({"goal": "income", "horizon_years": 10}, records(), bank_mode=False)["caps"]


def test_short_horizon_and_liquidity():
    out = suitability.check({"goal": "growth", "horizon_years": 2, "liquidity_needs": True},
                            records(vol=0.40, thin=True), bank_mode=False)
    assert [c["rule"] for c in out["caps"]] == ["short_horizon"]
    assert any("short horizon" in n for n in out["notes"]) and any("thinly" in n for n in out["notes"])


def test_missing_profile_fields_are_skipped():
    assert suitability.check({}, records(), bank_mode=False)["skipped"] == ["goal", "horizon"]


# --- Confidence ---------------------------------------------------------------------------------

def test_confidence_levels():
    assert confidence.assess(GOOD, "favourable", completeness_decision="proceed")["level"] == "high"
    mixed = {"business_quality": 1.3, "valuation": -2.0, "macro_fit": -0.2, "news": -0.1, "forecast": -1.0}
    assert confidence.assess(mixed, "neutral", completeness_decision="proceed")["level"] == "low"
    some = {"business_quality": 0.5, "valuation": -2.0, "macro_fit": 0.2, "news": 0.4, "forecast": -0.5}
    assert confidence.assess(some, "neutral", completeness_decision="proceed")["level"] == "medium"
    lowered = confidence.assess(GOOD, "favourable", completeness_decision="lower_confidence")
    assert lowered["level"] == "medium" and not lowered["calibrated"]


# --- The live cases -----------------------------------------------------------------------------

def test_apple_and_reliance_like_scores():
    # Version 2: Apple's valuation and forecast on the large-company bands (-1 each).
    apple = {"business_quality": 1.30, "valuation": -1.0, "macro_fit": -0.21, "news": -0.06, "forecast": -1.0}
    out = decide({"verified_records": records(), "user_profile": {"goal": "growth", "horizon_years": 10}}, apple)
    assert out["scorecard"]["score"] == pytest.approx(0.3155, abs=1e-3)
    assert out["preliminary_verdict"] == "neutral" and out["preliminary_confidence"] == "low"

    # Reliance: weak peers, so no forecast and the valuation at half weight.
    reliance = {"business_quality": 0.25, "valuation": -2.0, "macro_fit": 0.18, "news": 0.55, "forecast": None}
    weak = {"peers": {"comparisons": {"pe": {"premium": 1.23}, "ev_ebitda": {"premium": 1.89}}}}
    out = decide({"verified_records": records(), "user_profile": {"goal": "growth", "horizon_years": 10},
                  "valuation": weak}, reliance)
    assert out["scorecard"]["score"] == pytest.approx(-0.0256, abs=1e-3)
    assert out["preliminary_verdict"] == "neutral" and out["preliminary_confidence"] == "low"


def test_weak_peer_comparison_lowers_confidence():
    reliance = {"business_quality": 0.50, "valuation": -2.0, "macro_fit": 0.18, "news": 0.44, "forecast": -2.0}
    weak = {"peers": {"comparisons": {"pe": {"premium": 1.23}, "ev_ebitda": {"premium": 1.89}}}}
    st = {"verified_records": records(), "user_profile": {"goal": "growth", "horizon_years": 10}, "valuation": weak}
    out = decide(st, reliance)
    assert out["preliminary_verdict"] == "neutral" and out["preliminary_confidence"] == "low"
    assert out["scorecard"]["low_reliability"] == ["valuation", "forecast"]
    assert "weak peer comparison" in out["scorecard"]["confidence"]["reasons"][-1]

    normal = {"peers": {"comparisons": {"pe": {"premium": 0.38}}}}
    assert decide({**st, "valuation": normal}, reliance)["scorecard"]["low_reliability"] == []
