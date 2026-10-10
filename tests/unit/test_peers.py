"""
Unit tests for build step 7b, peer comparison: the Indian Trendlyne parser on its real
multi-stock answer, the US frames logic, the medians, and peers inside the verification
node and the filings server's cache.

All sources are fakes or saved real answers, so no network or quota is used. Run with:
    uv run pytest tests/unit/test_peers.py -v
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from invest_agent.cache.store import CacheStore
from invest_agent.mcp_servers.filings import peers_india, peers_us, server
from invest_agent.storage.db import connect
from invest_agent.verification import peers

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trendlyne"

# The industry search answer from scripts/peer_probe_india.py (5 October), shortened.
SEARCH = """status: success
data:
  name | nse_code | bse_code | isin | entity_type | sector | industry
  Panama Petrochem Ltd. | PANAMAPET | 524820 | INE305C01029 | stock | Oil & Gas | Refineries/Petro-Products
  Bharat Petroleum Corporation Ltd. | BPCL | 500547 | INE029A01011 | stock | Oil & Gas | Refineries/Petro-Products
  Hindustan Petroleum Corporation Ltd. | HINDPETRO | 500104 | INE094A01015 | stock | Oil & Gas | Refineries/Petro-Products
  Rama Petrochemicals Ltd. | RAMAPETRO | 500358 | INE783A01013 | stock | Commercial Services & Supplies | Commodity Trading  & Distribution
  Reliance Industries Ltd. | RELIANCE | 500325 | INE002A01018 | stock | Oil & Gas | Refineries/Petro-Products
"""


# --- India ------------------------------------------------------------------------------------

def test_real_multi_stock_answer_parses():
    values = peers_india.parse_multi_values((FIXTURES / "peers_values.json").read_text(encoding="utf-8"))
    assert values["RELIANCE"] == {"pb": 1.70, "pe": 21.15}
    assert values["BPCL"] == {"pb": 1.33, "pe": 7.61} and values["HINDPETRO"]["pe"] == 43.92


def test_industry_rows_keep_only_the_same_industry():
    rows = peers_india.parse_industry_rows(SEARCH, "Refineries/Petro-Products")
    assert [r["nse_code"] for r in rows] == ["PANAMAPET", "BPCL", "HINDPETRO", "RELIANCE"]


def test_india_peer_multiples_with_a_fake_trendlyne():
    calls = []

    async def fake(tool, args):
        calls.append((tool, args))
        if tool == "search_entities":
            return SEARCH
        return (FIXTURES / "peers_values.json").read_text(encoding="utf-8")

    out = asyncio.run(peers_india.india_peer_multiples("RELIANCE", "Refineries/Petro-Products", call=fake))
    assert out["company"]["pe"] == 21.15
    assert [p["symbol"] for p in out["peers"]] == ["PANAMAPET", "BPCL", "HINDPETRO"]
    values_calls = [args for tool, args in calls if tool == "get_stock_parameter_values"]
    assert values_calls[0]["stock_codes"] == ["RELIANCE", "PANAMAPET", "BPCL", "HINDPETRO"]   # one values call
    # Only 3 industry peers, so the Oil & Gas sector was searched too (version 6); this
    # fixture has no other Oil & Gas companies, so nothing was added.
    assert calls[1] == ("search_entities", {"query": "Oil & Gas", "limit": 50}) and out["widened_to"] is None
    assert out["peers"][0]["pe"] is None                     # not in the saved answer: missing, not zero


SECTOR_SEARCH = """status: success
data:
  name | nse_code | bse_code | isin | entity_type | sector | industry
  ITC Ltd. | ITC | 500875 | X | stock | Food, Beverages & Tobacco | Cigarettes-Tobacco Products
  Tata Consumer Products Ltd. | TATACONSUM | 500800 | X | stock | Food, Beverages & Tobacco | Tea & Coffee
  Nestle India Ltd. | NESTLEIND | 500790 | X | stock | Food, Beverages & Tobacco | Packaged Foods
  Some BSE Only Co. | 11330381 | 11330381 | X | stock | Food, Beverages & Tobacco | Packaged Foods
  Godfrey Phillips India Ltd. | GODFRYPHLP | 500163 | X | stock | Food, Beverages & Tobacco | Cigarettes-Tobacco Products
  Some Bank | SBIN | 500112 | X | stock | Banking | Banks
"""

INDUSTRY_SEARCH = """status: success
data:
  name | nse_code | bse_code | isin | entity_type | sector | industry
  ITC Ltd. | ITC | 500875 | X | stock | Food, Beverages & Tobacco | Cigarettes-Tobacco Products
  Godfrey Phillips India Ltd. | GODFRYPHLP | 500163 | X | stock | Food, Beverages & Tobacco | Cigarettes-Tobacco Products
  VST Industries Ltd. | VSTIND | 509966 | X | stock | Food, Beverages & Tobacco | Cigarettes-Tobacco Products
"""


def test_small_industry_widens_to_the_sector():
    calls = []

    async def fake(tool, args):
        calls.append((tool, args))
        if tool == "search_entities":
            return INDUSTRY_SEARCH if args["query"] == "Cigarettes-Tobacco Products" else SECTOR_SEARCH
        return '{"status": "success", "data": "PE TTM\\nITC:25.0\\nTATACONSUM:70.0"}'

    out = asyncio.run(peers_india.india_peer_multiples("ITC", "Cigarettes-Tobacco Products", call=fake))
    symbols = [p["symbol"] for p in out["peers"]]
    assert symbols == ["GODFRYPHLP", "VSTIND", "TATACONSUM", "NESTLEIND"]   # industry first, no repeats,
    assert out["widened_to"] == "Food, Beverages & Tobacco"                 # no bank, no BSE-only number
    assert "widened_to_sector" in out["flags"] and "Food, Beverages & Tobacco sector" in out["basis"]


# --- US ---------------------------------------------------------------------------------------

def test_frame_year_and_multiples():
    assert peers_us.frame_year(date(2026, 10, 5)) == 2025 and peers_us.frame_year(date(2026, 2, 1)) == 2024
    m = peers_us.multiples(1000.0, {"net_income": 50.0, "total_equity": 200.0, "revenue": 400.0,
                                    "operating_income": 80.0, "depreciation_amortization": 20.0,
                                    "long_term_debt": 100.0, "cash": 50.0})
    assert m == {"pe": 20.0, "pb": 5.0, "ps": 2.5, "ev_ebitda": pytest.approx(10.5)}
    assert peers_us.multiples(1000.0, {"net_income": -5.0})["pe"] is None


def test_us_peers_from_fake_fmp_and_frames():
    frames_asked = []

    async def fake_peers(symbol):
        return [{"symbol": "MSFT", "name": "Microsoft", "market_value": 3000.0},
                {"symbol": "GOOGL", "name": "Alphabet A", "market_value": 2000.0},
                {"symbol": "GOOG", "name": "Alphabet C", "market_value": 2000.0},       # same company
                {"symbol": "ZZZZ", "name": "Unknown", "market_value": 5.0}]             # not an SEC filer

    async def fake_frame(tag, unit, period, taxonomy="us-gaap"):
        frames_asked.append((tag, period))
        if tag == "NetIncomeLoss":
            return {"789019": 100.0, "1652044": 80.0}
        if tag == "RevenueFromContractWithCustomerExcludingAssessedTax":
            return {"789019": 300.0}                                     # Alphabet missing: next tag
        if tag == "Revenues":
            return {"1652044": 350.0}
        return {}

    ciks = {"AAPL": "320193", "MSFT": "789019", "GOOGL": "1652044", "GOOG": "1652044"}
    out = asyncio.run(peers_us.us_peer_multiples("AAPL", date(2026, 10, 5), fake_peers, fake_frame, lambda: ciks))
    assert [p["symbol"] for p in out["peers"]] == ["MSFT", "GOOGL"]
    assert out["peers"][0]["pe"] == 30.0 and out["peers"][1]["ps"] == pytest.approx(2000 / 350)
    assert ("NetIncomeLoss", "CY2025") in frames_asked and ("StockholdersEquity", "CY2025Q4I") in frames_asked
    assert out["basis"] == "SEC frames CY2025; market value from FMP"


# --- Medians -------------------------------------------------------------------------------------

def peer(symbol, pe, pb=2.0, ev=10.0, ps=3.0, value=None):
    return {"symbol": symbol, "pe": pe, "pb": pb, "ev_ebitda": ev, "ps": ps, "market_value": value,
            "figures": {"net_income": 1.0}}


def test_medians_premium_and_records():
    result = {"peers": [peer("A", 10), peer("B", 20), peer("C", 30), peer("D", -5), peer("E", 40), peer("F", 50)],
              "basis": "test"}
    out = peers.compare({"pe": 44.0, "pb": 3.0, "ev_ebitda": 5.0, "ps": 3.0}, result, ticker="AAPL", market="US",
                        bank_mode=False, as_of="2026-10-05", year=2025)
    pe = out["comparisons"]["pe"]
    assert pe["median"] == 30 and pe["peers_used"] == 5             # the loss-maker is left out
    assert pe["premium"] == pytest.approx(44 / 30 - 1)              # 47% dearer
    assert out["comparisons"]["ev_ebitda"]["premium"] == pytest.approx(-0.5)
    assert out["records"][0]["record_id"] == "computed:AAPL:peer_median_pe:FY2025"
    assert "peers: A, B, C, E, F" in out["records"][0]["source_ref"]


def test_too_few_peers_and_bank_mode():
    out = peers.compare({"pe": 10.0}, {"peers": [peer("A", 10), peer("B", 12)]}, ticker="X", market="US", bank_mode=False)
    assert out["status"] == "too_few_peers" and "only 2 peers" in out["not_computed"]["pe"]
    assert peers.which_multiples("IN", True) == ["pb", "pe"]
    assert peers.compare({}, {"peers": []}, ticker="X", market="IN", bank_mode=False)["status"] == "no_peers"


def test_india_compares_trendlyne_values_for_the_company():
    result = {"company": {"pe": 21.15, "pb": 1.7}, "peers": [peer(s, v, pb=1.4) for s, v in
                                                            zip("ABCDE", [7.6, 43.9, 12.0, 9.0, 15.0])]}
    out = peers.compare({"pe": 19.9}, result, ticker="RELIANCE", market="IN", bank_mode=False)
    assert out["comparisons"]["pe"]["company"] == 21.15 and out["comparisons"]["pe"]["median"] == 12.0


# --- Server cache ------------------------------------------------------------------------------

def test_server_caches_peer_answers(monkeypatch):
    calls = []

    async def fake_us(ticker):
        calls.append(ticker)
        return {"peers": [peer("MSFT", 35)], "basis": "x", "flags": []}

    monkeypatch.setattr(server.peers_us, "us_peer_multiples", fake_us)
    cache = CacheStore(connect(":memory:"))
    asyncio.run(server.peers_logic("US", "AAPL", cache=cache))
    again = asyncio.run(server.peers_logic("US", "AAPL", cache=cache))
    assert calls == ["AAPL"] and "from_cache" in again["flags"]


def test_20f_peers_and_small_peers_are_left_out():
    big = [peer(s, pe, value=v) for s, pe, v in
           [("GOOGL", 25, 4.1e12), ("META", 28, 1.9e12), ("MSFT", 35, 3.9e12), ("NVDA", 50, 5.7e12), ("NXT", 20, 1.2e10)]]
    small = [peer("RIME", 10, value=3e8), peer("TBCH", 8, value=2.5e8)]
    sony = {**peer("SONY", 18, value=1.5e11), "figures": {}}               # 20-F filer: no SEC figures
    out = peers.compare({"pe": 43.9}, {"peers": big + small + [sony]}, ticker="AAPL", market="US", bank_mode=False,
                        company_value=4.92e12, currency="USD")
    # 2% of $4.92 trillion is $98 billion: only 4 peers clear it, so the 5 largest are kept.
    assert out["peers"] == ["GOOGL", "META", "MSFT", "NVDA", "NXT"]
    assert {e["symbol"]: e["reason"] for e in out["excluded"]} == {
        "SONY": "no SEC figures (likely files 20-F)", "RIME": "too small to compare with", "TBCH": "too small to compare with"}
    assert out["comparisons"]["pe"]["median"] == 28


def test_indian_floor_keeps_peers_above_the_floor():
    rows = [peer(s, pe, value=v * 1e7) for s, pe, v in
            [("BPCL", 7.6, 130000), ("HINDPETRO", 43.9, 90000), ("MRPL", 15, 25000), ("PCBL", 30, 15000),
             ("CHENNPETRO", 6, 10000), ("GANDHAR", 18, 2000), ("GOACARBON", 9, 500)]]
    kept, excluded = peers.size_filter(rows, 16.4e12, "INR")
    assert [p["symbol"] for p in kept] == ["BPCL", "HINDPETRO", "MRPL", "PCBL", "CHENNPETRO"]
    assert {e["symbol"] for e in excluded} == {"GANDHAR", "GOACARBON"}
