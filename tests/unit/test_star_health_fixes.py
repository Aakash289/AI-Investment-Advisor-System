"""
tests/unit/test_star_health_fixes.py: the fixes from the Star Health and Eternal runs (6 October 2026).

What this file checks:
    the return target       read into words ("10% over 2 years, about 4.9% a year") and set
                            against the fair value range and the stock's swings
    margin trend            up against old loss years but falling recently: averaged to 0
    margin range            a pandemic-loss margin left out of the low and high cases
    latest year unusual     Eternal's FY2026 jump: flagged, and no fair value from it
    display                 a negative amount reads -₹1,119 crore; the 5-year note when prices
                            start later; no free cash flow row for an insurer; a confidence
                            drop by the guardrail is explained

Built on Star Health's and Eternal's real figures from the reports. No network.
"""

from __future__ import annotations

import pytest

from invest_agent.analysis.fundamental_score import business_quality
from invest_agent.analysis.projection import project
from invest_agent.analysis.target import describe, target_lines
from invest_agent.common.records import make_record
from invest_agent.fx.convert import format_large, format_money
from invest_agent.reporting.report_data import _history, _price_note
from invest_agent.verification.record_index import RecordIndex
from tests.unit.test_projection import YEARS, company, rec

STAR_REVENUE = [5051e7, 10610e7, 12102e7, 14026e7, 16113e7, 17834e7]
STAR_NET_MARGIN = [-0.215, -0.098, 0.051, 0.060, 0.040, 0.031]
STAR_OP_MARGIN = [-0.281, -0.128, 0.075, 0.083, 0.057, 0.045]


def star(**kw):
    return company(revenues=STAR_REVENUE, net_margins=STAR_NET_MARGIN, **kw) + \
        [rec("operating_margin", m, y) for y, m in zip(YEARS, STAR_OP_MARGIN)]


# --- Return target ----------------------------------------------------------------------------

def test_target_in_words():
    t = describe({"target_return_pct": 10, "target_return_basis": "total", "target_years": 2})
    assert t["text"] == "10% over 2 years (about 4.9% a year)" and t["annual"] == pytest.approx(0.0488, abs=1e-4)
    t = describe({"target_return_pct": 12, "target_return_basis": "per_year", "horizon_years": 3})
    assert t["text"] == "12% a year for 3 years (about 40.5% in total)"
    assert describe({"horizon_years": 2}) is None


def test_target_against_the_fair_value():
    state = {"user_profile": {"target_return_pct": 10, "target_return_basis": "total", "target_years": 2},
             "fair_value": {"status": "ok", "upside": {"low": -0.27, "base": 0.098, "high": 0.143}},
             "verified_records": [make_record(source="t", tier=2, entity="X", field="volatility_1y", value=0.235,
                                              as_of="2026-10-06")]}
    lines = target_lines(state)
    assert lines[0] == "You asked for 10% over 2 years (about 4.9% a year)."
    assert "+10% from today's price, above the 4.9% a year" in lines[1] and "low case is -27%" in lines[1]
    assert "swings about 24% a year" in lines[2] and "bank deposits" in lines[2]
    assert target_lines({"user_profile": {}}) == []


def test_target_without_a_fair_value_uses_past_growth():
    state = {"user_profile": {"target_return_pct": 10, "target_years": 2},
             "fair_value": {"status": "none", "reason": "no peer median"},
             "verified_records": [make_record(source="t", tier=2, entity="X", field="price_cagr_5y", value=0.195,
                                              as_of="2026-10-06")]}
    assert target_lines(state)[1].startswith("There is no fair value, because no peer median. So the analysis")
    assert "+19.5% a year" in target_lines(state)[1]


# --- Margins ----------------------------------------------------------------------------------

def test_margin_trend_averaged_when_recent_direction_disagrees():
    out = business_quality(star(), bank_mode=True)
    assert out["components"]["margin_trend"] == 0             # +2 against COVID years, -2 since FY2024
    text = next(f["text"] for f in out["findings"] if f["component"] == "margin_trend")
    assert "moved from 8.3% in FY2024 to 4.5% in FY2026" in text and "averaged" in text


def test_steady_improvement_keeps_its_score():
    margins = [0.05, 0.06, 0.07, 0.10, 0.11, 0.12]
    rows = company() + [rec("operating_margin", m, y) for y, m in zip(YEARS, margins)]
    assert business_quality(rows, bank_mode=False)["components"]["margin_trend"] == 2


def test_pandemic_margin_left_out_of_the_range():
    p = project(star())
    nm = p["assumptions"]["net_margin"]
    assert nm["low"] == pytest.approx(0.031) and nm["high"] == pytest.approx(0.060)   # FY2022's -9.8% left out
    assert p["assumptions"]["margin_left_out_years"] == {"FY2022": pytest.approx(-0.098)}
    assert "unusual_margin_years" in p["flags"]


def test_latest_year_unusual_is_flagged():
    eternal = [2118e7, 4687e7, 7761e7, 12961e7, 21320e7, 55760e7]
    p = project(company(revenues=eternal, net_margins=[-0.38, -0.26, -0.125, 0.027, 0.025, 0.007]))
    assert "latest_year_unusual" in p["flags"] and "FY2026" in p["assumptions"]["left_out_years"]
    assert "latest_year_unusual" not in project(star())["flags"]


# --- Display ----------------------------------------------------------------------------------

def test_negative_amounts_put_the_sign_first():
    assert format_large(-1119e7, "INR") == "-₹1,119 crore"
    assert format_money(-628.5, "USD") == "-$628.50"


def test_price_note_when_prices_start_later():
    chart = {"dates": ["2021-12-10", "2026-10-02"], "years": 4.8}
    assert _price_note(chart, RecordIndex([])).startswith("Prices start in December 2021 (about 4.8 years)")
    five = RecordIndex([make_record(source="t", tier=2, entity="X", field="return_5y", value=0.5, as_of="2026")])
    assert _price_note(chart, five) is None


def test_no_free_cash_flow_row_for_an_insurer():
    labels = [row[0] for row in _history(RecordIndex(star()), "INR", bank=True)["table"]]
    assert "Free cash flow" not in labels and "ROE" in labels
    assert "Free cash flow" in [row[0] for row in _history(RecordIndex(star()), "INR", bank=False)["table"]]


def test_latest_year_unusual_gives_no_fair_value():
    from invest_agent.analysis.step import analyse
    from tests.unit.test_projection import peers
    eternal = [2118e7, 4687e7, 7761e7, 12961e7, 21320e7, 55760e7]
    rows = company(revenues=eternal, net_margins=[0.02] * 6, shares=9e9, price=329.0)
    out = analyse({"verified_records": rows, "listing_currency": "INR", "ticker": "ETERNAL",
                   "valuation": {"peers": peers(pe=60.0, ev_ebitda=40.0)}})
    assert out["fair_value"]["status"] == "none" and out["fair_value"]["latest_year_unusual"]
    assert "FY2026: +161.5%" in out["fair_value"]["reason"] and out["sub_scores"]["forecast"] is None


# --- The target read in code ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("I have Rs 1000000, I want 10% returns in next 2 years, should i invest in Star Health Insurance",
     {"target_return_pct": 10.0, "target_return_basis": "total", "target_years": 2.0}),
    ("looking for 12% a year return over 5 years", {"target_return_pct": 12.0, "target_return_basis": "per_year",
                                                     "target_years": 5.0}),
    ("can it give returns of 15 percent annually?", {"target_return_pct": 15.0, "target_return_basis": "per_year"}),
    ("I want a 20% gain within 18 months", {"target_return_pct": 20.0, "target_return_basis": "total",
                                            "target_years": 1.5}),
    ("I want to make sure no more than 10% is in one stock", {}),
    ("is TCS good for long term growth", {}),
])
def test_target_parser(text, expected):
    from invest_agent.profile.target_parser import parse_target
    assert parse_target(text) == expected


def test_intake_fills_the_target_when_the_model_skips_it():
    import asyncio

    from invest_agent.agents.profile_intake import (
        ProfileExtraction,
        make_profile_intake_node,
    )

    class Store:
        def load(self, user_id="local"):
            return {"risk_tolerance": "low", "horizon_years": 2, "goal": "growth", "budget_amount": 1e6,
                    "budget_currency": "INR"}

        def save(self, profile, user_id="local"):
            pass

    async def model_skips(message):
        return ProfileExtraction(budget_text="Rs 1000000")

    msg = "I have Rs 1000000, I want 10% returns in next 2 years, should i invest in Star Health Insurance"
    out = asyncio.run(make_profile_intake_node(model_skips, Store)({"raw_input": msg}))
    assert out["user_profile"]["target_return_pct"] == 10.0 and out["user_profile"]["target_years"] == 2.0


def test_target_survives_a_question_about_the_company():
    """Turn 1 says the target, then asks which listing; turn 2 is only the answer. The
    profile carried from turn 1 keeps the target (main.CARRY_KEYS)."""
    import asyncio

    from invest_agent.agents.profile_intake import (
        ProfileExtraction,
        make_profile_intake_node,
    )
    from invest_agent.main import CARRY_KEYS

    assert "user_profile" in CARRY_KEYS

    class Store:
        def load(self, user_id="local"):
            return {"risk_tolerance": "low", "horizon_years": 2, "goal": "growth", "budget_amount": 1e6,
                    "budget_currency": "INR"}

        def save(self, profile, user_id="local"):
            pass

    async def reads_nothing(message):
        return ProfileExtraction()

    node = make_profile_intake_node(reads_nothing, Store)
    turn1 = asyncio.run(node({"raw_input": "I want 10% returns in next 2 years, should i invest in Star Health"}))
    turn2 = asyncio.run(node({"raw_input": "the NSE one", "user_profile": turn1["user_profile"]}))
    assert turn2["user_profile"]["target_return_pct"] == 10.0 and turn2["user_profile"]["target_years"] == 2.0
