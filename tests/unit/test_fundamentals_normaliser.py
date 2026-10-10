"""
Unit tests for build step 5, fundamentals: the SEC normaliser, the Trendlyne parser
(against Trendlyne's real answer for Reliance, saved on 5 October), the FMP mapping,
the measures, bank mode, the server's source order and cache, and the agent node.
No network or quota is used. Run with:
    uv run pytest tests/unit/test_fundamentals_normaliser.py -v
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from invest_agent.agents.fundamentals import build_records, make_fundamentals_node
from invest_agent.cache.store import CacheStore
from invest_agent.mcp_servers.filings import server
from invest_agent.mcp_servers.filings.fmp import map_statements
from invest_agent.mcp_servers.filings.metrics import compute_metrics
from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts
from invest_agent.mcp_servers.filings.trendlyne import (
    latest_fiscal_year, parse_entity_search, parse_parameter_values,
)
from invest_agent.storage.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trendlyne"
OCT_2026 = date(2026, 10, 5)


# --- SEC normaliser -------------------------------------------------------------------

def annual(val, end, filed, start=None, form="10-K", accn="0000-1"):
    e = {"val": val, "end": end, "filed": filed, "form": form, "accn": accn}
    if start:
        e["start"] = start
    return e


def year(y):
    return f"{y - 1}-10-01", f"{y}-09-30"           # an Apple-style year ending September


def facts_fixture():
    revenues_old, revenues_new = [], []
    for y in range(2018, 2022):                      # old tag up to 2021
        s, e = year(y)
        revenues_old.append(annual(100.0 * y, e, f"{y}-11-01", start=s))
    for y in range(2022, 2026):                      # the company switched tag in 2022
        s, e = year(y)
        revenues_new.append(annual(110.0 * y, e, f"{y}-11-01", start=s))
    s, e = year(2024)
    revenues_new.append(annual(999.0, e, "2025-11-01", start=s))                  # restated later: wins
    revenues_new.append(annual(5.0, "2025-06-30", "2025-08-01", start="2025-04-01", form="10-Q"))  # quarter: ignored
    return {"facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": revenues_old}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": revenues_new}},
            "StockholdersEquity": {"units": {"USD": [annual(500.0, "2025-09-30", "2025-11-01"),
                                                     annual(450.0, "2024-09-30", "2025-11-01")]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [annual(15e9, "2025-10-17", "2025-11-01")]}}},
    }}


def test_sec_normaliser_keeps_six_full_years_across_a_tag_switch():
    out = normalise_company_facts(facts_fixture())
    revenue = out["revenue"]
    assert [r["fiscal_year"] for r in revenue] == [2025, 2024, 2023, 2022, 2021, 2020]   # six, newest first
    assert revenue[0]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert revenue[-1]["tag"] == "Revenues"                                           # old tag fills old years
    assert revenue[1]["value"] == 999.0                                               # latest filing wins
    assert out["total_equity"][0]["value"] == 500.0 and out["shares_outstanding"][0]["value"] == 15e9


# --- Trendlyne parser, with the real answer for Reliance -------------------------------

def test_trendlyne_parser_reads_the_real_reliance_answer():
    text = (FIXTURES / "reliance_values_1.json").read_text(encoding="utf-8")
    figures, flags = parse_parameter_values(text, "RELIANCE", today=OCT_2026)
    net = {r["fiscal_year"]: r["value"] / 1e7 for r in figures["net_income"]}       # back to Rs crore
    assert net == {2026: 80775, 2025: 69648, 2024: 69621, 2023: 66702, 2022: 60705, 2021: 49128}
    assert len(figures["revenue"]) == 6 and figures["revenue"][0]["value"] == 1085997 * 1e7
    assert figures["ebitda"][0]["value"] == 178949 * 1e7                            # "Operating Profit" is EBITDA
    assert "unmapped_label:EBITDA Ann." in flags                                      # not a code we request


def test_trendlyne_parser_latest_only_codes_and_non_money():
    text = (FIXTURES / "reliance_values_2.json").read_text(encoding="utf-8")
    figures, _ = parse_parameter_values(text, "RELIANCE", today=OCT_2026)
    assert figures["eps_basic"][0]["value"] == 59.69                                  # not multiplied by a crore
    assert figures["long_term_debt"][0]["value"] == 270751 * 1e7
    assert "capex" not in figures                                                     # "None" stays missing
    assert [r["fiscal_year"] for r in figures["total_equity"]] == [2026, 2025, 2024, 2023, 2022, 2021]


def test_latest_indian_fiscal_year():
    assert latest_fiscal_year(date(2026, 10, 5)) == 2026
    assert latest_fiscal_year(date(2026, 4, 20)) == 2025       # FY2026 results not all out yet


def test_entity_search_row():
    text = ("status: success\ndata:\n  name | nse_code | bse_code | isin | entity_type | sector | industry\n"
            "  Reliance Industries Ltd. | RELIANCE | 500325 | INE002A01018 | stock | Oil & Gas | Refineries/Petro-Products")
    assert parse_entity_search(text, "RELIANCE")["industry"] == "Refineries/Petro-Products"


# --- FMP mapping ------------------------------------------------------------------------

def test_fmp_mapping_matches_the_sec_shape():
    out = map_statements({
        "income-statement": [{"date": "2025-09-27", "fiscalYear": "2025", "revenue": 416e9, "netIncome": 112e9}],
        "cash-flow-statement": [{"date": "2025-09-27", "fiscalYear": "2025", "operatingCashFlow": 111e9,
                                 "capitalExpenditure": -12e9}],
    })
    assert out["revenue"][0] == {**out["revenue"][0], "fiscal_year": 2025, "value": 416e9, "unit": "USD"}
    assert out["capex"][0]["value"] == 12e9                    # paid out: made positive like SEC's tag


# --- Measures -----------------------------------------------------------------------------

def reliance_figures():
    figures = {}
    for n in (1, 2):
        part, _ = parse_parameter_values((FIXTURES / f"reliance_values_{n}.json").read_text(encoding="utf-8"),
                                         "RELIANCE", today=OCT_2026)
        figures.update(part)
    return figures


def by_year(rows):
    return {r["fiscal_year"]: r["value"] for r in rows}


def test_measures_on_reliance():
    m = compute_metrics(reliance_figures())
    # EBIT = operating profit (EBITDA, Indian style) - depreciation: 178,949 - 57,688 crore
    assert by_year(m["operating_income"])[2026] == pytest.approx((178949 - 57688) * 1e7)
    assert by_year(m["roe"])[2026] == pytest.approx(80775 / 904030)
    assert by_year(m["debt_to_equity"])[2026] == pytest.approx((270751 + 103670) / 904030)   # matches Trendlyne's 0.41
    assert by_year(m["interest_coverage"])[2026] == pytest.approx((178949 - 57688) / 27061)
    assert m["revenue_cagr"][0]["years"] == 5 and m["revenue_cagr"][0]["value"] > 0.15
    assert "free_cash_flow" not in m                          # no capex in the Trendlyne answer


def test_bank_mode_skips_lender_measures():
    m = compute_metrics(reliance_figures(), bank_mode=True)
    assert "roe" in m and "debt_to_equity" not in m and "interest_coverage" not in m


def test_bank_detection():
    assert server.is_bank("US", 6021, None) and not server.is_bank("US", 6798, None)
    assert server.is_bank("IN", None, "Banks - Private Sector") and server.is_bank("IN", None, "Finance (including NBFCs)")
    assert not server.is_bank("IN", None, "Refineries/Petro-Products")


def test_indian_share_count_is_checked_against_eps():
    figures = reliance_figures()
    figures["shares_outstanding"] = [{**figures["eps_basic"][0], "value": 1353.2}]   # reported in crore
    flags = []
    server.settle_indian_shares(figures, flags)
    assert figures["shares_outstanding"][0]["value"] == pytest.approx(1353.2e7) and flags == []


# --- Server source order and cache --------------------------------------------------------

def test_us_falls_back_to_fmp_when_sec_fails(monkeypatch):
    async def fake_tracked(service, op, fn, *args, **kwargs):
        if service == "sec_edgar":
            raise RuntimeError("SEC down")
        return {"revenue": [{"fiscal_year": y, "period_end": f"{y}-09-30", "value": 1.0, "unit": "USD"} for y in (2025, 2024, 2023)],
                "net_income": [{"fiscal_year": y, "period_end": f"{y}-09-30", "value": 1.0, "unit": "USD"} for y in (2025, 2024, 2023)]}
    monkeypatch.setattr(server, "tracked_call", fake_tracked)
    cache = CacheStore(connect(":memory:"))
    out = asyncio.run(server.fundamentals_logic("US", "AAPL", "320193", 3571, cache=cache))
    assert out["source"] == "fmp" and "used_fallback" in out["flags"] and "sec_failed" in out["flags"]
    again = asyncio.run(server.fundamentals_logic("US", "AAPL", "320193", 3571, cache=cache))
    assert "from_cache" in again["flags"]                     # second ask costs no calls


# --- The agent node -------------------------------------------------------------------------

def test_node_builds_traceable_records():
    figures = reliance_figures()
    result = {"source": "trendlyne", "tier": 2, "currency": "INR", "figures": figures,
              "metrics": compute_metrics(figures), "bank_mode": False, "industry": "Refineries/Petro-Products"}

    async def fake_fetch(market, ticker, cik, sic, company_name=None):
        return result
    out = asyncio.run(make_fundamentals_node(fake_fetch)({"ticker": "RELIANCE", "market": "IN"}))
    records = {r["record_id"]: r for r in out["fundamentals_records"]}
    assert records["trendlyne:RELIANCE:net_income:FY2026"]["value"] == 80775 * 1e7
    roe = records["computed:RELIANCE:roe:FY2026"]
    assert "trendlyne:RELIANCE:net_income:FY2026" in roe["source_ref"]       # traceable to its inputs
    assert out["industry"] == "Refineries/Petro-Products" and out["bank_mode"] is False
