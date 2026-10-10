"""
Unit tests for build step 7a, the verification layer: sanity checks, cross-source
conflicts, completeness, the small-company rule, the company's own multiples and the
verification node.

Built on Trendlyne's real Reliance answers (tests/fixtures/trendlyne/) turned into
records by the real Fundamentals Agent code, plus small made-up records for the edge
cases. No network. Run with:
    uv run pytest tests/unit/test_verification.py -v
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from invest_agent.agents.fundamentals import build_records
from invest_agent.common.records import make_record
from invest_agent.mcp_servers.filings import server
from invest_agent.mcp_servers.filings.metrics import compute_metrics
from invest_agent.mcp_servers.filings.trendlyne import parse_parameter_values
from invest_agent.verification import completeness, coverage, cross_source, sanity, valuation
from invest_agent.verification.step import verify as verify_async

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trendlyne"
TODAY = date(2026, 10, 5)


async def no_peers(market, ticker, industry):
    return {"peers": [], "flags": []}


def verify(state, today, fetch_peers=no_peers):
    """The node is async (it calls the peer server); run it with a fake peer source."""
    return asyncio.run(verify_async(state, today, fetch_peers))


# --- Reliance, built the way the live agent builds it -------------------------------------

def reliance_records():
    figures, flags = {}, []
    for n in (1, 2):
        part, _ = parse_parameter_values((FIXTURES / f"reliance_values_{n}.json").read_text(encoding="utf-8"),
                                         "RELIANCE", today=TODAY)
        figures.update(part)
    server.settle_indian_shares(figures, flags)
    # Capex comes from yfinance in the live run; the four years it gave on 5 October.
    figures["capex"] = [{"fiscal_year": y, "period_end": f"{y}-03-31", "value": v * 1e7, "unit": "INR",
                         "tag": "yfinance:Capital Expenditure"}
                        for y, v in ((2026, 122916), (2025, 131107), (2024, 152883), (2023, 141809))]
    result = {"source": "trendlyne", "tier": 2, "currency": "INR", "figures": figures,
              "metrics": compute_metrics(figures)}
    return build_records(result, "RELIANCE")


def price(value=1186.40, as_of="2026-10-05", ticker="RELIANCE", unit="INR", **extra):
    records = [make_record(source="yfinance", tier=2, entity=ticker, field="current_price", value=value,
                           as_of=as_of, period=as_of, unit=unit)]
    records.append(make_record(source="yfinance", tier=2, entity=ticker, field="volatility_1y", value=0.208,
                               as_of=as_of, period=as_of, unit="ratio"))
    return records


MACRO_OK = {"home_macro": True, "panel_macro": True, "news": True, "geographic_split": True,
            "sector": "Energy"}   # what the other agents supply
LABELS = {"rates": "steady", "inflation": "near_target", "growth": "steady"}
PANEL = {"home": "IN", "countries": {c: {"name": c, "indicators": {}, "regimes": dict(LABELS)}
                                    for c in ("US", "IN", "CN", "EA", "JP", "GB")}}


def reliance_state(**over):
    state = {"ticker": "RELIANCE", "market": "IN", "listing_currency": "INR", "company_name": "Reliance Industries Limited",
             "price_records": price(), "fundamentals_records": reliance_records(), "fx_records": [],
             "user_profile": {"budget_amount": 200_000, "budget_currency": "INR"},
             "sector": "Energy", "bank_mode": False, "macro_panel": PANEL}
    state.update(over)
    return state


def rec(field, value, year=2026, source="trendlyne", tier=2, ticker="X", **extra):
    return make_record(source=source, tier=tier, entity=ticker, field=field, value=value, as_of=f"{year}-03-31",
                       period=f"FY{year}", fiscal_year=year, period_end=f"{year}-03-31", **extra)


# --- Sanity ------------------------------------------------------------------------------------

def test_range_check_excludes_impossible_figures():
    kept, findings = sanity.range_check([rec("gross_margin", 1.2), rec("shares_outstanding", 0), rec("net_margin", 0.07)])
    assert [r["field"] for r in kept] == ["net_margin"]
    assert {f["field"] for f in findings} == {"gross_margin", "shares_outstanding"}


def test_reliance_passes_every_check():
    out = sanity.check_records(reliance_records() + price(), ticker="RELIANCE", listing_currency="INR",
                               reporting_currency="INR", today=TODAY)
    assert out["findings"] == []                                # EPS within 5%, fresh, no gaps
    assert out["unbroken_years"] == [2026, 2025, 2024, 2023, 2022, 2021]


def test_eps_mismatch_is_flagged_and_recomputed():
    records = [rec("revenue", 100.0), rec("net_income", 10.0), rec("eps_basic", 2.0), rec("shares_outstanding", 10.0)]
    out = sanity.check_records(records, ticker="X", listing_currency="INR", reporting_currency="INR", today=TODAY)
    assert out["findings"][0]["check"] == "eps"
    added = next(r for r in out["records"] if r["field"] == "eps_recomputed")
    assert added["value"] == 1.0 and "eps_mismatch" in added["flags"]


def test_stale_price_and_old_accounts():
    old_price = price(as_of="2026-09-01")                      # 25 business days before 5 October
    out = sanity.check_records(old_price + [rec("revenue", 1.0, year=2025)], ticker="X", listing_currency="INR",
                               reporting_currency="INR", today=TODAY)
    assert not any(r["field"] == "current_price" for r in out["records"])          # counted as missing
    assert {f["action"] for f in out["findings"]} >= {"counted as missing", "flagged stale"}   # FY2025 ends 18+ months ago


def test_gap_in_the_years_is_recorded():
    records = [rec("revenue", 1.0, year=y) for y in (2026, 2025, 2023, 2022)]
    out = sanity.check_records(records, ticker="X", listing_currency="INR", reporting_currency="INR", today=TODAY)
    assert out["unbroken_years"] == [2026, 2025]
    assert out["findings"][0]["detail"] == "no figures for FY2024"


def test_reporting_currency_differs_is_flagged():
    findings = sanity.currency_check(sanity.RecordIndex(price(unit="USD")), "USD", "EUR")
    assert findings[0]["detail"] == "reports in EUR, trades in USD"


# --- Cross-source --------------------------------------------------------------------------

def test_close_values_keep_the_higher_tier_without_a_conflict():
    kept, conflicts = cross_source.resolve([rec("revenue", 100.0, source="fmp", tier=2),
                                            rec("revenue", 100.3, source="sec_edgar", tier=1)])
    assert conflicts == [] and kept[0]["source"] == "sec_edgar"


def test_units_mistake_is_recognised():
    kept, conflicts = cross_source.resolve([rec("revenue", 1_085_997.0, source="other", tier=2),
                                            rec("revenue", 1_085_997e7, source="trendlyne", tier=1)])
    assert conflicts[0]["resolution"] == "units_corrected" and kept[0]["value"] == 1_085_997e7


def test_real_conflict_goes_to_the_higher_tier_and_is_logged():
    kept, conflicts = cross_source.resolve([rec("net_income", 90.0, source="fmp", tier=2),
                                            rec("net_income", 100.0, source="sec_edgar", tier=1)])
    assert kept[0]["value"] == 100.0 and conflicts[0]["resolution"] == "higher_tier_wins"
    assert conflicts[0]["gap"] == pytest.approx(0.1)


# --- Completeness -------------------------------------------------------------------------

def test_reliance_is_complete_and_known_gaps_are_listed():
    out = completeness.score(reliance_records() + price(), market="IN", bank_mode=False,
                             extra={"budget": 200_000, "sector": "Energy", "peer_medians": True, **MACRO_OK})
    assert out["decision"] == "proceed" and out["score"] == 1.0 and out["missing_critical"] == []
    assert "india_bond_yield" in out["by_design_gaps"]
    assert out["pending_until_built"] == {}                 # every agent is built
    assert out["fields"]["news"]["present"] and out["fields"]["geographic_split"]["present"]


def test_recent_ipo_and_missing_budget_withhold_the_amount():
    records = [rec("revenue", 1.0, year=y) for y in (2026, 2025)] + [rec("net_income", 1.0, year=y) for y in (2026, 2025)]
    out = completeness.score(records + price(), market="IN", bank_mode=False, extra={"budget": None})
    assert out["decision"] == "withhold_amount"
    assert {"revenue", "net_income", "budget", "shares_outstanding", "total_equity"} <= set(out["missing_critical"])


def test_missing_free_cash_flow_lowers_confidence_but_not_for_a_bank():
    records = [r for r in reliance_records() if r["field"] != "free_cash_flow"] + price()
    extra = {"budget": 1, **MACRO_OK, "news": False}        # no news and no peers either: 80% for a company
    assert completeness.score(records, market="IN", bank_mode=False, extra=extra)["decision"] == "lower_confidence"
    assert completeness.score(records, market="IN", bank_mode=True, extra=extra)["decision"] == "proceed"   # 87.5%


# --- Small-company rule -----------------------------------------------------------------------

def test_small_company_is_not_covered():
    small = coverage.check_market_value(price(value=10.0) + [rec("shares_outstanding", 1e8)], "INR")
    assert small["covered"] is False and "₹1,00,00,00,000" in small["reason"]       # ₹100 crore < ₹500 crore
    assert coverage.check_market_value(price(), "INR")["flag"] == "market_value_unknown"


# --- Own multiples ----------------------------------------------------------------------------

def test_reliance_multiples():
    dividend = rec("dividend_per_share_1y", 6.0, ticker="RELIANCE")       # ₹6, from Trendlyne check 3
    out = valuation.own_multiples(reliance_records() + price() + [dividend], ticker="RELIANCE", bank_mode=False,
                                  listing_currency="INR", reporting_currency="INR")
    m = out["multiples"]
    market_value = 1186.40 * 1353.25e7
    assert m["market_value"] == pytest.approx(market_value, rel=1e-3)
    assert m["pe"] == pytest.approx(market_value / (80775 * 1e7), rel=1e-3)          # about 19.9
    assert m["pb"] == pytest.approx(market_value / (904030 * 1e7), rel=1e-3)
    assert "ev_ebitda" in m and m["dividend_yield"] == pytest.approx(6 / 1186.40)
    pe = next(r for r in out["records"] if r["field"] == "pe")
    assert pe["record_id"] == "computed:RELIANCE:pe:FY2026" and "trendlyne:RELIANCE:net_income:FY2026" in pe["source_ref"]


def test_loss_maker_bank_and_currency_mismatch():
    records = price(value=100.0, ticker="X") + [rec("shares_outstanding", 1e9), rec("net_income", -5e9), rec("revenue", 1e11),
                                                rec("total_equity", 5e10), rec("ebitda", 1e10, source="computed"),
                                                rec("total_debt", 1e10, source="computed"), rec("cash", 1e9)]
    out = valuation.own_multiples(records, ticker="X", bank_mode=True, listing_currency="INR", reporting_currency="INR")
    assert "pe" not in out["multiples"] and out["not_meaningful"]["pe"] == "loss-making in the latest year"
    assert "ev_ebitda" not in out["multiples"] and out["multiples"]["pb"] == pytest.approx(2.0)
    adr = valuation.own_multiples(records, ticker="X", bank_mode=False, listing_currency="USD", reporting_currency="EUR")
    assert adr["status"] == "currency_mismatch" and adr["multiples"] == {}


# --- The node ------------------------------------------------------------------------------------

def test_node_on_reliance():
    out = verify(reliance_state(), TODAY)
    assert out["completeness_detail"]["decision"] == "proceed" and out["amount_withheld"] is False
    assert any(r["record_id"] == "computed:RELIANCE:pe:FY2026" for r in out["verified_records"])
    assert out["completeness_detail"]["user_line"].startswith("The company's figures come from an aggregator")
    assert any(n.startswith("Known gaps, by design") for n in out["data_notes"])


def test_node_ends_a_small_company_request():
    state = reliance_state(price_records=price(value=0.3))      # 1,353 crore shares at ₹0.30: about ₹406 crore
    out = verify(state, TODAY)
    assert out["verdict"] == "not_covered"
    assert out["final_response"].startswith("Reliance Industries Limited is not covered in version 1 because its market value")


def test_graph_stops_after_verification_for_a_small_company():
    from invest_agent.graph.build_graph import build_graph

    async def entity(state):
        return {"entity_status": "resolved", "ticker": "RELIANCE", "exchange": "NSE", "market": "IN",
                "listing_currency": "INR", "company_name": "Tiny Co"}

    async def intake(state):
        return {"user_profile": {"risk_tolerance": "low", "horizon_years": 10, "goal": "growth",
                                 "budget_amount": 200_000, "budget_currency": "INR"}}

    async def prices(state):
        return {"price_records": price(value=0.3)}

    async def fundamentals(state):
        return {"fundamentals_records": reliance_records()}

    async def macro(state):
        return {"macro_records": []}

    async def news(state):
        return {"news_records": []}

    async def geo(state):
        return {"geo_sector_records": []}

    graph = build_graph({"entity_resolution": entity, "profile_intake": intake, "technical_price": prices,
                         "fundamentals": fundamentals, "macro": macro, "news": news, "geo_sector": geo})
    result = asyncio.run(graph.ainvoke({"raw_input": "x"}))
    assert result["verdict"] == "not_covered" and "Tiny Co is not covered in version 1" in result["final_response"]


def test_node_compares_with_peers_and_counts_them_as_present():
    async def fake_peers(market, ticker, industry):
        assert (market, ticker, industry) == ("IN", "RELIANCE", "Refineries/Petro-Products")
        values = [("BPCL", 7.61, 1.33), ("HINDPETRO", 43.92, 1.38), ("MRPL", 15.0, 1.6), ("CHENNPETRO", 6.0, 1.1),
                  ("GANDHAR", 18.0, 2.0)]
        return {"company": {"pe": 21.15, "pb": 1.70}, "basis": "Trendlyne",
                "peers": [{"symbol": s, "pe": pe, "pb": pb, "ev_ebitda": None} for s, pe, pb in values]}

    out = verify(reliance_state(industry="Refineries/Petro-Products"), TODAY, fake_peers)
    comparisons = out["valuation"]["peers"]["comparisons"]
    assert comparisons["pe"]["median"] == 15.0 and comparisons["pe"]["company"] == 21.15
    assert "ev_ebitda" in out["valuation"]["peers"]["not_computed"]
    assert out["completeness_detail"]["fields"]["peer_medians"]["present"] is True
    assert any(r["field"] == "peer_median_pe" for r in out["verified_records"])
