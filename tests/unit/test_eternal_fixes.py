"""
tests/unit/test_eternal_fixes.py: the fixes from the Eternal run in the app (7 October 2026).

What this file checks:
    valuation fallback   with no P/E or EV/EBITDA peer median (most peers loss-making),
                         valuation is scored on P/B and P/S instead, marked as a fallback;
                         the scorecard then counts it at half weight, lowers confidence one
                         step with the right reason, and the report says why
    banks                keep P/B as their main measure and never use the fallback
    growth cap           when the base growth is above the 25% cap, the low case is kept at
                         least 10 points below it instead of all three cases being 25%; the
                         report explains it; a normal grower is unchanged
    local time           stored UTC times are shown in the computer's own time zone
"""

from __future__ import annotations

import pytest

from invest_agent.analysis import fundamental_score, scorecard
from invest_agent.analysis.projection import project
from invest_agent.analysis.step import decide
from invest_agent.app import view
from invest_agent.reporting.report_data import projection_notes

from .test_projection import company
from .test_scorecard import records


def comps(**premiums):
    return {"comparisons": {m: {"company": 10.0, "median": 5.0, "premium": p, "peers_used": 5}
                            for m, p in premiums.items()}}


# --- Valuation fallback -------------------------------------------------------------------------

def test_pb_is_used_when_pe_and_ev_ebitda_have_no_median():
    out = fundamental_score.valuation(comps(pb=0.83), bank_mode=False, large=True)
    assert out["score"] == -2 and out["fallback"] == ["pb"]
    assert "too few peers have a P/E or EV/EBITDA" in out["findings"][0]["text"]


def test_no_fallback_when_pe_is_there():
    out = fundamental_score.valuation(comps(pe=0.05, pb=0.83), bank_mode=False)
    assert out["fallback"] == [] and list(out["components"]) == ["pe"]


def test_banks_use_pb_as_usual():
    out = fundamental_score.valuation(comps(pb=0.05), bank_mode=True)
    assert out["fallback"] == [] and out["components"] == {"pb": -0.25}       # 5% dearer, sliding


def test_nothing_at_all_is_still_missing():
    assert fundamental_score.valuation(comps(), bank_mode=False)["score"] is None


def test_fallback_valuation_is_low_reliability_and_lowers_confidence():
    subs = {"business_quality": 0.4, "valuation": -2.0, "macro_fit": -0.5, "news": 0.88, "forecast": None}
    st = {"verified_records": records(), "user_profile": {"goal": "growth", "horizon_years": 2}}
    out = decide(st, subs, valuation_fallback=["pb"])
    card = out["scorecard"]
    assert card["low_reliability"] == ["valuation"]
    assert scorecard.reliability_reason(card, "valuation").startswith("only P/B could be compared with peers")
    assert any("valuation is low reliability (only P/B" in r for r in card["confidence"]["reasons"])
    # Half weight: the same scores without the fallback mark give a lower weighted score.
    plain = decide(st, subs)["scorecard"]
    assert card["score"] > plain["score"]


def test_report_says_why_the_valuation_is_low_reliability():
    from invest_agent.reporting import report_data

    from .test_report_integrity import state as report_state

    s = report_state()
    s["scorecard"] = {"score": 0.1, "raw_verdict": "neutral", "weights_used": {}, "low_reliability": ["valuation"],
                      "low_reliability_reasons": {"valuation": "only P/B could be compared with peers"}}
    s["valuation"] = {"peers": comps(pb=0.83)}
    v = report_data.build(s)["valuation"]
    assert v["weak"] is False and v["fallback_note"].startswith("Low reliability: only P/B")
    # The old weak-peer case keeps its own box.
    s["scorecard"]["low_reliability_reasons"] = {"valuation": scorecard.WEAK_PEERS_REASON}
    v = report_data.build(s)["valuation"]
    assert v["weak"] is True and v["fallback_note"] is None


# --- Growth cap ---------------------------------------------------------------------------------

def test_capped_growth_keeps_a_range():
    p = project(company(growth=0.60))
    g = p["assumptions"]["growth"]
    assert g["base"] == 0.25 and g["high"] == 0.25
    assert g["low"] == pytest.approx(0.15)                     # at least 10 points below the cap
    assert "growth_capped_base" in p["flags"]
    notes = projection_notes({"projection": p}, p["assumptions"])
    assert any("above the cap of 25.0%" in n and "at least 10 points" in n for n in notes)


def test_normal_growth_is_unchanged():
    p = project(company(growth=0.10))
    g = p["assumptions"]["growth"]
    assert g["base"] == pytest.approx(0.10) and not any(f.startswith("growth_capped") for f in p["flags"])


# --- Local time ---------------------------------------------------------------------------------

def test_utc_shown_in_local_time(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time zone cannot be switched on this system")
    time.tzset()
    try:
        assert view.local_time("2026-10-07T12:34:00+00:00") == "07 Oct 2026, 18:04"
        assert view.local_time(None) == ""
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# --- Spread check fixes (7 October, later) ------------------------------------------------------

def test_high_confidence_needs_four_sub_scores():
    from invest_agent.analysis.confidence import assess

    two = {"business_quality": None, "valuation": 0.0, "macro_fit": 0.0, "news": None, "forecast": None}
    out = assess(two, "neutral", completeness_decision="proceed")          # Pfizer on 7 October
    assert out["level"] == "low" and "only 2 of the 5" in out["reasons"][-1]
    three = {"business_quality": 0.2, "valuation": 0.1, "macro_fit": 0.0, "news": None, "forecast": None}
    assert assess(three, "neutral", completeness_decision="proceed")["level"] == "medium"
    four = {**three, "news": 0.3}
    assert assess(four, "neutral", completeness_decision="proceed")["level"] == "high"


def _sec(tag_values: dict, taxonomy: str = "us-gaap") -> dict:
    """A tiny SEC company facts file: {tag: {fiscal_year: value}} as 10-K annual entries."""
    facts: dict = {}
    for tag, years in tag_values.items():
        tax, name = tag.split(":", 1) if ":" in tag else (taxonomy, tag)
        unit = "shares" if "Shares" in name else "USD"
        entries = []
        for y, v in years.items():
            e = {"end": f"{y}-12-31", "val": v, "form": "10-K", "filed": f"{y + 1}-02-15", "accn": f"a{y}"}
            if "Shares" not in name:
                e["start"] = f"{y}-01-01"
            entries.append(e)
        facts.setdefault(tax, {})[name] = {"units": {unit: entries}}
    return {"facts": facts}


def test_operating_income_rebuilt_from_pretax_income():
    from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts

    years = range(2020, 2026)
    facts = _sec({"Revenues": {y: 100.0 for y in years},
                  "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
                      {y: 20.0 for y in years},
                  "InterestExpense": {y: 3.0 for y in years},
                  "OperatingIncomeLoss": {2020: 25.0}})
    op = {r["fiscal_year"]: r for r in normalise_company_facts(facts)["operating_income"]}
    assert op[2025]["value"] == 23.0 and op[2025]["tag"].startswith("rebuilt:")
    assert op[2020]["value"] == 25.0 and op[2020]["tag"] == "OperatingIncomeLoss"   # reported years kept


def test_share_count_from_the_balance_sheet_tag():
    from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts

    facts = _sec({"Revenues": {2025: 1.0}, "us-gaap:CommonStockSharesOutstanding": {2025: 12e9}})
    assert normalise_company_facts(facts)["shares_outstanding"][0]["value"] == 12e9


def test_us_shares_fall_back_to_eps_then_yahoo(monkeypatch):
    import asyncio

    from invest_agent.mcp_servers.filings import server

    figs = {"net_income": [{"fiscal_year": 2025, "period_end": "2025-12-31", "value": 100.0}],
            "eps_diluted": [{"fiscal_year": 2025, "value": 8.0}]}
    flags: list = []
    asyncio.run(server.settle_us_shares(figs, "GOOGL", flags))
    assert figs["shares_outstanding"][0]["value"] == 12.5 and flags == ["shares_derived_from_eps"]

    async def fake_tracked(service, op, fn, *args, **kwargs):
        return 12.1e9

    monkeypatch.setattr(server, "tracked_call", fake_tracked)
    figs = {"net_income": [{"fiscal_year": 2025, "period_end": "2025-12-31", "value": 100.0}]}
    flags = []
    asyncio.run(server.settle_us_shares(figs, "GOOGL", flags))
    assert figs["shares_outstanding"][0]["value"] == 12.1e9 and flags == ["shares_from_yfinance"]


def test_unfavourable_line_at_minus_half():
    # Scorecard version 5 (7 October): unfavourable at -0.5 or below, favourable still +0.75.
    assert scorecard.verdict_from(-0.58) == "unfavourable"          # Vodafone Idea in the spread check
    assert scorecard.verdict_from(-0.48) == "neutral"               # Ford
    assert scorecard.verdict_from(0.74) == "neutral" and scorecard.verdict_from(0.75) == "favourable"


def test_depreciation_rebuilt_from_its_parts():
    from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts

    years = range(2021, 2027)
    facts = _sec({"Revenues": {y: 300.0 for y in years}, "Depreciation": {y: 20.0 for y in years},
                  "AmortizationOfIntangibleAssets": {y: 5.0 for y in years if y > 2021}})
    out = normalise_company_facts(facts)
    da = {r["fiscal_year"]: r for r in out["depreciation_amortization"]}
    assert da[2026]["value"] == 25.0 and da[2021]["value"] == 20.0 and da[2026]["tag"].startswith("rebuilt:")
    assert "depreciation_only" not in out and "intangible_amortization" not in out
