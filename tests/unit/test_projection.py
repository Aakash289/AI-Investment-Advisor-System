"""
tests/unit/test_projection.py: forecasting and fair value (design 14.10), with made-up figures.

What this file checks:
    projection   steady growth gives a narrow range; growth is capped at +25% and
                 flagged; a one-off jump is flagged; margins come from the company's own
                 range; book value grows by earnings less dividends; too little history
                 means no projection
    fair value   P/E and EV/EBITDA averaged; a loss-maker valued on EV/EBITDA alone;
                 both negative means none; no peer median means none (no fallback to
                 the company's own multiple); P/B for banks; a currency mismatch means none
    F4 score     the bands, and missing when there is no fair value
    the node     forecast sub-score, and every projected number tagged "projected"

No network: every input is built in the test.
"""

from __future__ import annotations

import pytest

from invest_agent.analysis import forecast_score
from invest_agent.analysis.fair_value import fair_value
from invest_agent.analysis.forecasting import forecast
from invest_agent.analysis.projection import project
from invest_agent.analysis.step import analyse
from invest_agent.common.records import make_record

YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def rec(field, value, year=None):
    extra = {"fiscal_year": year, "period": f"FY{year}"} if year else {}
    return make_record(source="test", tier=1, entity="X", field=field, value=value, as_of="2026-03-31", **extra)


def company(growth=0.10, net_margins=None, ebitda_margin=0.20, shares=10.0, price=20.0, net_debt=50.0,
            equity=500.0, dividends=-4.0, revenues=None):
    """Revenue starting at 100 and growing `growth` a year unless `revenues` is given."""
    revenues = revenues or [100 * (1 + growth) ** i for i in range(6)]
    net_margins = net_margins or [0.10] * 6
    rows = []
    for y, rev, nm in zip(YEARS, revenues, net_margins):
        rows += [rec("revenue", rev, y), rec("net_income", rev * nm, y), rec("ebitda", rev * ebitda_margin, y)]
    rows += [rec("shares_outstanding", shares), rec("current_price", price), rec("net_debt", net_debt, 2026),
             rec("total_equity", equity, 2026), rec("dividends_paid", dividends, 2026)]
    return rows


def peers(**medians):
    return {"comparisons": {m: {"median": v, "company": 1.0, "premium": 0.0, "peers_used": 5}
                            for m, v in medians.items()}}


# --- Projection ---------------------------------------------------------------------------------

def test_steady_grower_has_a_narrow_range():
    p = project(company())
    a = p["assumptions"]
    assert a["trend_growth"] == pytest.approx(0.10)
    assert a["growth_sd"] == pytest.approx(0.0, abs=1e-9)
    base = p["cases"]["base"][2027]
    assert base["revenue"] == pytest.approx(100 * 1.1 ** 6)
    assert base["eps"] == pytest.approx(100 * 1.1 ** 6 * 0.10 / 10)
    assert p["years"] == [2027, 2028]
    # Book value: 500 + year-1 earnings - 4 of dividends.
    assert base["book_value"] == pytest.approx(500 + 100 * 1.1 ** 6 * 0.10 - 4)


def test_margins_come_from_the_companys_own_range():
    p = project(company(net_margins=[0.05, 0.12, 0.08, 0.10, 0.09, 0.11]))
    m = p["assumptions"]["net_margin"]
    assert m["low"] == pytest.approx(0.08) and m["high"] == pytest.approx(0.12)     # last 5 years
    assert m["base"] == pytest.approx((0.11 + 0.09 + 0.10) / 3)                      # last 3 years


def test_fast_growth_is_capped_and_flagged():
    p = project(company(growth=0.40))
    assert p["assumptions"]["growth"]["base"] == 0.25
    assert "growth_capped_base" in p["flags"]


def test_one_unusual_year_is_flagged_and_left_out_of_the_spread():
    # Apple's real revenue FY2020 to FY2025 ($ billion): the FY2021 jump of 33% is the odd year.
    revenues = [274.5, 365.8, 394.3, 383.3, 391.0, 416.2]
    p = project(company(revenues=revenues))
    a = p["assumptions"]
    assert "one_unusual_year" in p["flags"] and a["spread_years_left_out"] == 1
    assert a["growth_sd"] == pytest.approx(0.048, abs=0.002)        # not 0.14 with FY2021 in
    # The base growth is the last 3 usual years (FY2023 to FY2025): about 1.8% a year.
    # The longer record, with FY2021 left out, is about 3.3%.
    assert a["growth"]["base"] == pytest.approx(0.018, abs=0.001) and a["growth_method"] == "recent_years"
    assert a["long_run_growth"] == pytest.approx(0.033, abs=0.001)
    assert a["growth"]["high"] < 0.09


def test_reliance_low_base_year_is_left_out_of_growth():
    from invest_agent.analysis.fundamental_score import usual_growth
    pts = list(zip(range(2021, 2027), [483251, 714909, 889569, 917121, 982671, 1085997]))
    g = usual_growth(pts)
    assert list(g["left_out"]) == [2022] and g["growth"] == pytest.approx(0.110, abs=0.002)


def test_too_little_history_means_no_projection():
    p = project([rec("revenue", 100, 2025), rec("revenue", 110, 2026)])
    assert p["status"] == "not_enough_history"


# --- Fair value ---------------------------------------------------------------------------------

def test_pe_and_ev_ebitda_are_averaged():
    rows = company()
    p = project(rows)
    fv = fair_value(p, peers(pe=15.0, ev_ebitda=8.0), rows, bank_mode=False)
    rev1 = 100 * 1.1 ** 6
    pe_value = rev1 * 0.10 / 10 * 15.0
    ev_value = (rev1 * 0.20 * 8.0 - 50.0) / 10
    assert fv["methods"]["pe"]["base"] == pytest.approx(pe_value)
    assert fv["methods"]["ev_ebitda"]["base"] == pytest.approx(ev_value)
    assert fv["range"]["base"] == pytest.approx((pe_value + ev_value) / 2)
    assert fv["upside"]["base"] == pytest.approx(fv["range"]["base"] / 20.0 - 1)


def test_loss_maker_uses_ev_ebitda_alone():
    rows = company(net_margins=[-0.05] * 6)
    fv = fair_value(project(rows), peers(pe=15.0, ev_ebitda=8.0), rows, bank_mode=False)
    assert list(fv["methods"]) == ["ev_ebitda"]
    assert "projected earnings are negative" in fv["flags"]


def test_both_negative_means_no_fair_value():
    rows = company(net_margins=[-0.05] * 6, ebitda_margin=-0.02)
    fv = fair_value(project(rows), peers(pe=15.0, ev_ebitda=8.0), rows, bank_mode=False)
    assert fv["status"] == "none"


def test_no_peer_median_means_no_fair_value():
    rows = company()
    fv = fair_value(project(rows), peers(pb=2.0), rows, bank_mode=False)
    assert fv["status"] == "none" and "no peer median for forward P/E" in fv["reason"]


def test_bank_uses_book_value():
    rows = company()
    p = project(rows)
    fv = fair_value(p, peers(pe=15.0, pb=1.5), rows, bank_mode=True)
    assert list(fv["methods"]) == ["pb"]
    assert fv["range"]["base"] == pytest.approx(p["cases"]["base"][2027]["book_value_per_share"] * 1.5)


def test_currency_mismatch_means_no_fair_value():
    rows = company()
    fv = fair_value(project(rows), peers(pe=15.0), rows, bank_mode=False, currency_mismatch=True)
    assert fv["status"] == "none"


# --- F4 score -----------------------------------------------------------------------------------

# Version 6: the score slides between the middles of the bands (scoring_common.smooth_band).
@pytest.mark.parametrize("upside,score", [(0.30, 2), (0.25, 2), (0.15, 0.86), (0.0, 0), (-0.10, -0.57),
                                          (-0.20, -1.33), (-0.25, -2), (-0.40, -2)])
def test_forecast_bands(upside, score):
    fv = {"status": "ok", "range": {"base": 100 * (1 + upside), "low": 90.0, "high": 140.0}, "price": 100.0,
          "upside": {"base": upside}}
    assert forecast_score.forecast(fv, money=lambda v: f"${v:.2f}")["score"] == score


def test_no_fair_value_is_missing():
    out = forecast_score.forecast({"status": "none", "reason": "no peer median"}, money=str)
    assert out["score"] is None and out["missing"] == ["no peer median"]


# --- The node -----------------------------------------------------------------------------------

def test_every_projected_number_is_tagged():
    rows = company()
    out = forecast(rows, {"status": "ok", "peers": peers(pe=15.0, ev_ebitda=8.0)}, ticker="X", bank_mode=False,
                   unbroken_years=YEARS)
    assert out["records"] and all(r["source"] == "projected" and "projected" in r["flags"] for r in out["records"])
    fair = [r for r in out["records"] if r["field"] == "fair_value_base"]
    assert fair and "forward P/E at the peer median of 15.0" in fair[0]["source_ref"]


def test_analyse_adds_the_forecast_sub_score():
    rows = company(price=10.0)
    state = {"verified_records": rows, "valuation": {"status": "ok", "peers": peers(pe=15.0, ev_ebitda=8.0)},
             "ticker": "X", "market": "US", "listing_currency": "USD", "completeness_detail": {"unbroken_years": YEARS}}
    out = analyse(state)
    assert out["sub_scores"]["forecast"] == 2          # base value well above a price of 10
    assert out["fair_value"]["status"] == "ok"
    assert any(f["component"] == "forecast" and f["record_ids"] for f in out["findings"])


def test_weak_peers_mean_no_fair_value_and_no_forecast_score():
    rows = company(price=10.0)
    weak = {"status": "ok", "peers": {"comparisons": {"pe": {"median": 15.0, "company": 40.0, "premium": 1.6,
                                                              "peers_used": 5},
                                                       "ev_ebitda": {"median": 8.0, "company": 9.0, "premium": 0.1,
                                                                     "peers_used": 5}}}}
    out = analyse({"verified_records": rows, "valuation": weak, "ticker": "X", "market": "US",
                   "listing_currency": "USD", "completeness_detail": {"unbroken_years": YEARS}})
    assert out["fair_value"]["status"] == "none" and "P/E 160% above" in out["fair_value"]["reason"]
    assert out["sub_scores"]["forecast"] is None
    assert not any(r["field"].startswith("fair_value_") for r in out["projection_records"])
    assert out["scorecard"]["low_reliability"] == ["valuation"]


def test_reliance_base_growth_is_the_last_three_years():
    p = project(company(revenues=[483251, 714909, 889569, 917121, 982671, 1085997]))
    a = p["assumptions"]
    assert a["recent_years"] == ["FY2024", "FY2025", "FY2026"]
    assert a["growth"]["base"] == pytest.approx(0.069, abs=0.001)
