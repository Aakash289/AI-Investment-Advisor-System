"""
tests/unit/test_scores.py: the 9a scorers (design 14.18), with made-up figures.

What this file checks:
    scoring_common   band edges and the missing-weight rule
    business quality the worked example in 14.18 comes out at +1.45; bank mode skips
                     balance sheet and cash generation; net cash scores +2; the net debt
                     to EBITDA stand-in when interest coverage is missing
    valuation        premium bands, P/E and EV/EBITDA averaged, a loss-maker scored on
                     EV/EBITDA alone, P/B for banks
    macro fit        a Reliance-like case (rupee weaker, India growth slowing) and an
                     Apple-like case (several economies, rest of world ignored), plus the
                     Indian IT currency override
    news             net x 2, halved for thin coverage, missing when there is nothing
    the node         all four sub-scores and findings land in the state

No network: every input is built in the test.
"""

from __future__ import annotations

import pytest

from invest_agent.analysis import fundamental_score, macro_score, news_score
from invest_agent.analysis.scoring_common import band, combine
from invest_agent.analysis.step import analyse
from invest_agent.common.records import make_record

YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def rec(field, value, year, ticker="X"):
    return make_record(source="trendlyne", tier=2, entity=ticker, field=field, value=value, as_of=f"{year}-03-31",
                       period=f"FY{year}", fiscal_year=year, period_end=f"{year}-03-31")


def worked_example() -> list[dict]:
    """14.18: 11% growth, 17% ROIC, margin up 2 points, 12x coverage, FCF positive 5 of 6."""
    rows = []
    for i, y in enumerate(YEARS):
        rows.append(rec("revenue", 1000 * 1.11 ** i, y))
        rows.append(rec("roic", 0.17, y))
        rows.append(rec("operating_margin", 0.22 if y >= 2024 else 0.20, y))
        rows.append(rec("free_cash_flow", -5 if y == 2022 else 50, y))
    rows.append(rec("interest_coverage", 12, 2026))
    rows.append(rec("net_debt", 300, 2026))
    return rows


# --- Shared mechanics ---------------------------------------------------------------------------

def test_band_edges():
    bands = [[0.15, 2], [0.08, 1], [0.02, 0], [-0.02, -1], [None, -2]]
    assert band(0.15, bands) == 2
    assert band(0.1499, bands) == 1
    assert band(0.02, bands) == 0
    assert band(-0.05, bands) == -2


def test_combine_reweights_and_gives_up_past_half():
    w = {"a": 0.25, "b": 0.25, "c": 0.5}
    assert combine({"a": 2, "b": 0, "c": None}, w)["score"] == 1.0         # half missing: still scored
    assert combine({"a": 2, "b": None, "c": None}, w)["score"] is None     # 75% missing: missing


# --- Business quality ---------------------------------------------------------------------------

def test_worked_example_scores_plus_1_45():
    out = fundamental_score.business_quality(worked_example(), bank_mode=False)
    assert out["components"] == {"growth": 1, "profitability": 2, "margin_trend": 1,
                                 "balance_sheet": 2, "cash_generation": 1}
    assert out["score"] == pytest.approx(1.45)
    texts = " ".join(f["text"] for f in out["findings"])
    assert "11.0% a year" in texts and "ROIC averaged 17.0%" in texts
    assert all(f["record_ids"] for f in out["findings"])


def test_net_cash_scores_plus_2():
    rows = [r for r in worked_example() if r["field"] != "net_debt"] + [rec("net_debt", -50, 2026)]
    out = fundamental_score.business_quality(rows, bank_mode=False)
    assert out["components"]["balance_sheet"] == 2


def test_net_debt_to_ebitda_stands_in_without_coverage():
    rows = [r for r in worked_example() if r["field"] != "interest_coverage"] + [rec("net_debt_to_ebitda", 0.38, 2026)]
    out = fundamental_score.business_quality(rows, bank_mode=False)
    assert out["components"]["balance_sheet"] == 1
    assert any("not reported" in f["text"] for f in out["findings"])


def test_bank_mode_uses_roe_and_skips_two_parts():
    rows = [rec("revenue", 500 * 1.10 ** i, y) for i, y in enumerate(YEARS)]
    rows += [rec("roe", 0.16, y) for y in YEARS]
    out = fundamental_score.business_quality(rows, bank_mode=True)
    assert "balance_sheet" not in out["components"] and "cash_generation" not in out["components"]
    assert out["components"]["profitability"] == 2
    assert out["missing"] == ["margin_trend"]


def test_one_year_of_roic_is_used_and_said():
    rows = [r for r in worked_example() if not (r["field"] == "roic" and r["fiscal_year"] < 2026)]
    rows = [r if r["field"] != "roic" else rec("roic", 0.08, 2026) for r in rows]
    out = fundamental_score.business_quality(rows, bank_mode=False)
    assert out["components"]["profitability"] == 0
    assert any("only the latest year" in f["text"] for f in out["findings"])


def test_too_little_history_is_missing():
    out = fundamental_score.business_quality([rec("revenue", 100, 2026)], bank_mode=False)
    assert out["score"] is None


def test_trend_growth_ignores_one_odd_year():
    pts = [(2021, 100), (2022, 110), (2023, 60), (2024, 133), (2025, 146), (2026, 161)]
    g = fundamental_score.trend_growth(pts)
    assert 0.05 < g < 0.20


# --- Valuation ----------------------------------------------------------------------------------

def peers(**prem):
    return {"comparisons": {m: {"company": 20 * (1 + p), "median": 20.0, "peers_used": 5, "premium": p}
                            for m, p in prem.items()}}


def test_valuation_averages_pe_and_ev_ebitda():
    out = fundamental_score.valuation(peers(pe=0.38, ev_ebitda=0.05), bank_mode=False)
    # Version 6 slides between bands: 5% dearer is a quarter of the way from 0 to -1.
    assert out["components"] == {"pe": -2, "ev_ebitda": -0.25}
    assert out["score"] == -1.125


def test_loss_maker_uses_ev_ebitda_alone():
    p = peers(ev_ebitda=-0.35)
    p["comparisons"]["pe"] = {"company": -4.0, "median": 20.0, "peers_used": 5, "premium": None}
    out = fundamental_score.valuation(p, bank_mode=False)
    assert out["score"] == 2 and out["missing"] == ["pe"]


def test_bank_valuation_uses_pb():
    out = fundamental_score.valuation(peers(pe=0.5, pb=-0.15), bank_mode=True)
    assert out["components"] == {"pb": 0.75}                 # 15% cheaper, sliding towards +1


def test_no_peers_is_missing():
    assert fundamental_score.valuation(None, bank_mode=False)["score"] is None


# --- Macro fit ----------------------------------------------------------------------------------

def panel(**countries):
    return {"countries": {code: {"name": code, **c} for code, c in countries.items()}}


def test_reliance_like_macro_fit():
    p = panel(IN={"regimes": {"rates": "steady", "inflation": "near_target", "growth": "slowing"},
                  "indicators": {"currency": {"change_12m_pct": 8.1, "series": "DEXINUS"}}})
    out = macro_score.macro_fit(p, {"weights": {"IN": 0.636, "ROW": 0.364}}, sector="Energy",
                                industry="Oil & Gas Refining & Marketing", market="IN")
    # India: growth slowing -0.5 (Energy growth Medium), inflation counts 0 (Low).
    # Weighted -0.5 x 0.636 = -0.318; rupee weaker with 36% abroad adds +0.5 (currency Medium).
    assert out["score"] == pytest.approx(0.182)
    assert {f["component"] for f in out["findings"]} == {"macro_IN", "macro_currency"}


def test_apple_like_macro_fit_skips_small_and_row():
    p = panel(US={"regimes": {"rates": "steady", "inflation": "above_target", "growth": "steady",
                              "credit": "steady", "yield_curve": "normal"},
                  "indicators": {"currency": {"change_12m_pct": 0.5}}},
              EA={"regimes": {"rates": "rising", "inflation": "near_target", "growth": "steady"}},
              CN={"regimes": {"rates": "steady", "inflation": "below_target", "growth": "slowing"}},
              JP={"regimes": {"rates": "rising", "growth": "slowing"}})
    geo = {"weights": {"US": 0.429, "EA": 0.267, "CN": 0.155, "ROW": 0.081, "JP": 0.069}}
    out = macro_score.macro_fit(p, geo, sector="Information Technology", industry="Consumer Electronics", market="US")
    # EA rates rising -0.5 x 0.267, CN growth slowing -0.5 x 0.155; US 0; JP under 10%; dollar flat.
    assert out["score"] == pytest.approx(-0.211)
    assert "macro_JP" not in {f["component"] for f in out["findings"]}


def test_indian_it_exporter_gets_full_currency_weight():
    sens = macro_score.sensitivity("Information Technology", "IT Services & Consulting", "IN")
    assert sens["currency"] == "High"
    assert macro_score.sensitivity("Information Technology", "IT Services", "US")["currency"] == "Medium"


def test_banks_like_rising_rates():
    sens = macro_score.sensitivity("Financials", "Banks", "IN")
    s, _ = macro_score.country_score({"rates": "rising"}, sens, {"High": 1, "Medium": 0.5, "Low": 0})
    assert s == 1


def test_macro_missing_without_sector():
    out = macro_score.macro_fit(panel(IN={"regimes": {}}), {"weights": {"IN": 1.0}}, sector=None,
                                industry=None, market="IN")
    assert out["score"] is None and out["missing"] == ["sector"]


# --- News ---------------------------------------------------------------------------------------

def test_news_score_and_thin_coverage():
    full = news_score.news({"summary": {"net_sentiment": 0.30, "relevant_count": 6}})
    thin = news_score.news({"summary": {"net_sentiment": 0.30, "relevant_count": 2}})
    assert full["score"] == pytest.approx(0.6)
    assert thin["score"] == pytest.approx(0.3)
    assert news_score.news({"summary": {"relevant_count": 0}})["score"] is None


# --- The node -----------------------------------------------------------------------------------

def test_analyse_fills_the_state():
    state = {"verified_records": worked_example(), "valuation": {"peers": peers(pe=0.38, ev_ebitda=0.32)},
             "macro_panel": panel(IN={"regimes": {"growth": "slowing"}}), "geo_exposure": {"weights": {"IN": 1.0}},
             "sector": "Energy", "market": "IN", "news": {"summary": {"net_sentiment": 0.3, "relevant_count": 6}},
             "bank_mode": False, "completeness_detail": {"unbroken_years": YEARS}}
    out = analyse(state)
    assert out["sub_scores"]["business_quality"] == pytest.approx(1.45)
    assert out["sub_scores"]["valuation"] == -2
    assert out["sub_scores"]["macro_fit"] == pytest.approx(-0.5)
    assert out["sub_scores"]["news"] == pytest.approx(0.6)
    assert out["findings"]


def test_one_scorer_failing_leaves_the_rest():
    out = analyse({"verified_records": worked_example(), "valuation": {"peers": "broken"}, "market": "US"})
    assert out["sub_scores"]["valuation"] is None
    assert out["sub_scores"]["business_quality"] == pytest.approx(1.45)


def test_large_company_valuation_bands():
    """Version 2: a large company 38% and 32% dearer than its peers scores about -1, not -2
    (version 6: -1.2 and -0.9, sliding)."""
    from invest_agent.analysis.fundamental_score import is_large, valuation
    peers = {"comparisons": {"pe": {"company": 43.9, "median": 31.7, "premium": 0.38, "peers_used": 5},
                             "ev_ebitda": {"company": 34.4, "median": 26.0, "premium": 0.32, "peers_used": 5}}}
    assert valuation(peers, bank_mode=False)["score"] == -2
    big = valuation(peers, bank_mode=False, large=True)
    assert big["score"] == -1.05 and "large-company bands" in big["findings"][0]["text"]
    assert is_large({"multiples": {"market_value": 4.9e12}}, "USD")
    assert is_large({"multiples": {"market_value": 1.6e13}}, "INR")
    assert not is_large({"multiples": {"market_value": 9.0e11}}, "INR")
    assert not is_large({}, "USD")


def test_large_company_forecast_bands():
    from invest_agent.analysis.forecast_score import forecast
    fv = {"status": "ok", "upside": {"base": -0.30}, "range": {"low": 60, "base": 70, "high": 80}, "price": 100}
    assert forecast(fv, money=str)["score"] == -2
    assert forecast(fv, money=str, large=True)["score"] == -1.5      # version 6: sliding
