"""
Unit tests for build step 8a, the Macro agent: the FRED, BIS, IMF and World Bank parsers
and transforms, the regime rules, the panel builder with fake sources, and the node.

The sample answers are in the exact formats the macro probe saw on 5 October 2026. No
network. Run with:
    uv run pytest tests/unit/test_macro.py -v
"""

import asyncio
from datetime import date

import pytest

from invest_agent.agents.macro import build_records, make_macro_node
from invest_agent.cache.store import CacheStore
from invest_agent.mcp_servers.macro_data import bis, fred, imf, regime_rules, server, worldbank
from invest_agent.storage.db import connect

TODAY = date(2026, 10, 5)


def monthly(values, start_year=2025, start_month=1):
    rows, y, m = [], start_year, start_month
    for v in values:
        rows.append((f"{y}-{m:02d}-01", v))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return rows


# --- FRED ------------------------------------------------------------------------------------

def test_fred_parser_skips_empty_days():
    payload = {"observations": [{"date": "2026-10-02", "value": "0.45"}, {"date": "2026-10-03", "value": "."},
                                {"date": "2026-10-01", "value": "0.46"}]}
    assert fred.parse_observations(payload) == [("2026-10-01", 0.46), ("2026-10-02", 0.45)]


def test_fred_transforms():
    fed = monthly([4.33] * 3 + [4.0, 4.0, 3.75, 3.5, 3.5, 3.63, 3.75])
    assert fred.level(fed, 6)["change_6m"] == pytest.approx(3.75 - 4.0)
    cpi = monthly([100 + i * 0.25 for i in range(20)])
    assert fred.yoy(cpi)["value"] == pytest.approx((104.75 / 101.75 - 1) * 100, abs=1e-3)
    gdp = [("2025-01-01", 100.0), ("2025-04-01", 101.0), ("2025-07-01", 102.0), ("2025-10-01", 102.5),
           ("2026-01-01", 103.0), ("2026-04-01", 103.5)]
    q = fred.yoy_quarter(gdp)
    assert q["value"] == pytest.approx((103.5 / 101.0 - 1) * 100, abs=1e-3) and q["recent_two_quarters"] < q["previous_two_quarters"]
    assert fred.change_12m(monthly([90.0] + [95.0] * 12))["change_12m_pct"] == pytest.approx(5.5556, abs=1e-3)


# --- BIS, IMF, World Bank -------------------------------------------------------------------

BIS_CSV = """FREQ,REF_AREA,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE
D,IN,368,2026-01-15,5.50
D,IN,368,2026-03-02,5.25
D,IN,368,2026-07-22,5.25
D,JP,368,2026-01-15,0.50
D,JP,368,2026-07-22,0.75
"""


def test_bis_csv_and_six_month_change():
    rates = bis.parse_csv(BIS_CSV)
    assert rates["IN"][-1] == ("2026-07-22", 5.25)
    assert bis.latest_and_change(rates["IN"])["change_6m"] == pytest.approx(-0.25)      # 15 Jan: 5.50
    assert bis.latest_and_change(rates["JP"])["change_6m"] == pytest.approx(0.25)


def test_imf_and_world_bank_parsers():
    data = imf.parse({"values": {"NGDP_RPCH": {"IND": {"2025": 6.5, "2026": 6.2}, "USA": {"2026": 1.9}}}}, "NGDP_RPCH")
    assert imf.this_and_last_year(data["IND"], 2026) == {"value": 6.2, "date": "2026", "estimate": True, "previous": 6.5}
    wb = [{"page": 1}, [{"date": "2025", "value": 4.96}, {"date": "2024", "value": 5.0}]]
    assert worldbank.parse(wb) == [(2024, 5.0), (2025, 4.96)]


# --- Regime rules ----------------------------------------------------------------------------

def test_regime_rules():
    assert regime_rules.rates({"change_6m": 0.25}) == "rising" and regime_rules.rates({"change_6m": -0.5}) == "falling"
    assert regime_rules.rates({"change_6m": 0.1}) == "steady" and regime_rules.rates({}) is None
    assert regime_rules.inflation("IN", {"value": 5.2}) == "above_target"        # target 4, near within 1
    assert regime_rules.inflation("US", {"value": 2.9}) == "near_target"
    assert regime_rules.inflation("JP", {"value": 0.5}) == "below_target"
    assert regime_rules.growth({"recent_two_quarters": 1.0, "previous_two_quarters": 2.0}) == "slowing"
    assert regime_rules.growth({"value": 6.5, "previous": 6.0}) == "accelerating"
    assert regime_rules.credit({"change_3m": -0.3}) == "easing"
    assert regime_rules.yield_curve({"value": -0.1}) == "inverted" and regime_rules.yield_curve({"value": 0.45}) == "normal"


# --- Panel builder -----------------------------------------------------------------------------

class FakeSources:
    def __init__(self, imf_fails=False):
        self.imf_fails, self.calls = imf_fails, []

    async def fred_fetch(self, series_id, frequency=None, limit=30):
        self.calls.append(series_id)
        if series_id == "GDPC1":
            return [(f"{y}-{m:02d}-01", 100 + i) for i, (y, m) in enumerate(
                [(2025, 1), (2025, 4), (2025, 7), (2025, 10), (2026, 1), (2026, 4)])]
        if series_id == "FEDFUNDS":
            return monthly([4.33] * 4 + [4.0, 4.0, 3.75, 3.5, 3.5, 3.63, 3.75, 3.75, 3.75, 3.75, 3.75, 3.75,
                                         3.75, 3.75, 3.75, 3.75, 3.75], 2025, 1)
        return monthly([100 + i for i in range(21)], 2025, 1)

    async def bis_rates(self, codes, last_n=200):
        return bis.parse_csv(BIS_CSV)

    async def imf_indicator(self, code):
        if self.imf_fails:
            raise RuntimeError("IMF down")
        return {c: {2025: 6.5, 2026: 6.0} for c in ("IND", "CHN", "EURO", "JPN", "GBR")}

    async def wb_latest(self, country, indicator):
        return {"value": 4.96, "date": "2025", "lagged": True, "previous": 5.0}


def test_panel_from_fake_sources_with_labels_and_cache():
    cache = CacheStore(connect(":memory:"))
    src = FakeSources()
    out = asyncio.run(server.panel_logic(TODAY, cache, src))
    us, india = out["countries"]["US"], out["countries"]["IN"]
    assert us["indicators"]["policy_rate"]["source"] == "fred" and "yield_curve" in us["regimes"]
    assert india["indicators"]["policy_rate"] == {"value": 5.25, "date": "2026-07-22", "change_6m": -0.25, "source": "bis"}
    assert india["regimes"]["rates"] == "falling" and india["regimes"]["growth"] == "slowing"
    assert india["indicators"]["gdp_growth"]["estimate"] is True
    assert "change_12m_pct" in india["indicators"]["currency"]
    assert out["countries"]["CN"]["indicators"] and "policy_rate" not in out["countries"]["CN"]["indicators"]  # not in the sample
    again = asyncio.run(server.panel_logic(TODAY, cache, FakeSources()))
    assert "from_cache" in again["flags"]


def test_world_bank_steps_in_when_the_imf_fails():
    out = asyncio.run(server.panel_logic(TODAY, CacheStore(connect(":memory:")), FakeSources(imf_fails=True)))
    assert out["countries"]["CN"]["indicators"]["gdp_growth"]["source"] == "world_bank"
    assert "CN_gdp_growth_from_world_bank" in out["flags"] and "imf_gdp_growth_unavailable" in out["flags"]


# --- The node -----------------------------------------------------------------------------------

def test_node_records_and_failure():
    panel = asyncio.run(server.panel_logic(TODAY, CacheStore(connect(":memory:")), FakeSources()))
    records = build_records(panel)
    ids = {r["record_id"] for r in records}
    assert "bis:IN:policy_rate:2026-07-22" in ids
    regime = next(r for r in records if r["record_id"].startswith("computed:IN:regime_rates"))
    assert regime["value"] == "falling" and regime["source_ref"] == "bis:IN:policy_rate:2026-07-22"

    async def fake():
        return panel
    out = asyncio.run(make_macro_node(fake)({"market": "IN"}))
    assert out["macro_panel"]["home"] == "IN" and out["macro_records"]

    async def broken():
        raise RuntimeError("server down")
    assert asyncio.run(make_macro_node(broken)({"market": "US"}))["macro_panel"]["flags"] == ["macro_failed"]
