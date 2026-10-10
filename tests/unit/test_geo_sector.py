"""
Unit tests for build step 8c, the Geographic and Sector agent: member names to panel
codes, Apple's 10-K breakdowns (the rows scripts/geo_probe.py printed on 6 October 2026),
Reliance's within and outside India table, the sum check and home default, the sector
rules, the server with fake sources, and the node.

No network and no edgartools download. Run with:
    uv run pytest tests/unit/test_geo_sector.py -v
"""

import asyncio

import pytest

from invest_agent.agents.geo_sector import build_records, make_geo_sector_node
from invest_agent.cache.store import CacheStore
from invest_agent.mcp_servers.filings import exposure_checks, sector, segments, server, trendlyne_segments
from invest_agent.storage.db import connect

REV = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
GEO = "dim_srt_StatementGeographicalAxis"
SEG = "dim_us-gaap_StatementBusinessSegmentsAxis"
PROD = "dim_srt_ProductOrServiceAxis"
CONS = "dim_srt_ConsolidationItemsAxis"
OPSEG = "us-gaap:OperatingSegmentsMember"


def row(value, dims=None, end="2025-09-27", start="2024-09-29", concept=REV):
    r = {"concept": concept, "numeric_value": value, "period_end": end, "period_start": start,
         GEO: None, SEG: None, PROD: None, CONS: None}
    r.update(dims or {})
    return r


# Apple's FY2025 10-K, as the probe printed it (US dollars). Segment rows also carry
# ConsolidationItemsAxis = OperatingSegmentsMember, as in the real filing.
APPLE_ROWS = [
    row(416161e6),                                                    # total, no dimension
    row(151790e6, {GEO: "country:US"}), row(64377e6, {GEO: "country:CN"}),
    row(199994e6, {GEO: "aapl:OtherCountriesMember"}),
    row(178353e6, {SEG: "aapl:AmericasSegmentMember", CONS: OPSEG}), row(111032e6, {SEG: "aapl:EuropeSegmentMember", CONS: OPSEG}),
    row(64377e6, {SEG: "aapl:GreaterChinaSegmentMember", CONS: OPSEG}), row(28703e6, {SEG: "aapl:JapanSegmentMember", CONS: OPSEG}),
    row(33696e6, {SEG: "aapl:RestOfAsiaPacificSegmentMember", CONS: OPSEG}),
    row(209586e6, {PROD: "aapl:IPhoneMember"}),                       # product split: not geography
    row(167045e6, {SEG: "aapl:AmericasSegmentMember", CONS: OPSEG}, end="2024-09-28", start="2023-10-01"),   # older year
]

# Reliance's annual report answer from the probe (shortened, same layout).
RELIANCE_DOC = ('{"markdown_data": "\\"2177348|Reliance Industries Ltd.|RELIANCE|1127|Annual Report|2026-04-23\\\\n'
                '## (V) Secondary Segment Information\\\\n| | | 2025-26 | in crore) 2024-25 |\\\\n'
                '| | Within India | 7,48,203 | 6,86,534 |\\\\n| | Outside India | 4,27,716 | 3,84,640 |\\\\n'
                '| | Total | 11,75,919 | 10,71,174 |\\\\n'
                '2293441|Rolex Rings Ltd.|ROLEXRINGS|612873|Annual Report|2026-05-15\\\\n'
                '| | Within India | 500 | 400 |\\\\n| | Outside India | 700 | 600 |\\\\n"}')


# --- Names and weights ------------------------------------------------------------------------

def test_member_names_map_to_panel_codes():
    assert exposure_checks.tidy("aapl:RestOfAsiaPacificSegmentMember") == "rest of asia pacific"
    assert exposure_checks.region_code("country:US") == ("US", True)
    assert exposure_checks.region_code("aapl:GreaterChinaSegmentMember") == ("CN", True)
    assert exposure_checks.region_code("Within India") == ("IN", True)
    assert exposure_checks.region_code("aapl:LatinAmericaMember") == ("ROW", False)     # unknown: flagged


def test_apple_breakdowns_and_choice():
    found = segments.breakdowns(APPLE_ROWS)
    assert found["period_end"] == "2025-09-27" and found["total"] == 416161e6
    axes = {c["axis"]: c for c in found["candidates"]}
    assert set(axes) == {"StatementGeographicalAxis", "StatementBusinessSegmentsAxis"}
    assert len(axes["StatementBusinessSegmentsAxis"]["parts"]) == 5            # the older year left out
    chosen = exposure_checks.choose(found["candidates"])
    # Segments name 92% of revenue (US, EA, CN, JP); the country axis only 52% (US, CN).
    assert chosen["axis"] == "StatementBusinessSegmentsAxis" and chosen["sum_check"]["status"] == "ok"
    w = chosen["weights"]
    assert w["US"] == pytest.approx(178353 / 416161, abs=1e-4) and w["EA"] == pytest.approx(111032 / 416161, abs=1e-4)
    assert w["ROW"] == pytest.approx(33696 / 416161, abs=1e-4)


def test_failed_sum_check_and_home_default():
    bad = [{"axis": "x", "parts": [{"name": "us", "value": 50.0}], "reported_total": 100.0}]
    assert exposure_checks.choose(bad) is None                              # half the revenue is missing
    assert exposure_checks.home_default("IN", "none")["weights"] == {"IN": 1.0}


def test_reliance_within_and_outside_india():
    found = trendlyne_segments.parse_within_outside(RELIANCE_DOC, "RELIANCE")
    assert found["parts"] == [{"name": "Within India", "value": 748203.0}, {"name": "Outside India", "value": 427716.0}]
    assert found["reported_total"] == 1175919.0                               # Rolex Rings' table ignored
    chosen = exposure_checks.choose([found])
    assert chosen["weights"]["IN"] == pytest.approx(748203 / 1175919, abs=1e-4) and chosen["sum_check"]["status"] == "ok"


# --- Sector -----------------------------------------------------------------------------------------

def test_sector_rules():
    apple = sector.classify("US", 3571, "Technology", "Consumer Electronics")
    assert apple["sector"] == "Information Technology" and apple["source"] == "sic" and apple["flags"] == []
    reliance = sector.classify("IN", None, "Energy", "Oil & Gas Refining & Marketing")
    assert reliance["sector"] == "Energy" and reliance["source"] == "yahoo"
    clash = sector.classify("US", 2911, "Industrials", None)                 # refining SIC, Yahoo says otherwise
    assert clash["sector"] == "Energy" and clash["flags"][0].startswith("sector_disagreement")
    assert sector.from_sic(6021) == "Financials" and sector.from_sic(6798) == "Real Estate"


# --- Server and node ---------------------------------------------------------------------------------

class FakeGeo:
    calls: list = []

    @staticmethod
    async def us_rows(ticker):
        FakeGeo.calls.append(ticker)
        return {"accession": "0000320193-25-000079", "rows": APPLE_ROWS}

    @staticmethod
    async def india(ticker, name):
        return trendlyne_segments.parse_within_outside(RELIANCE_DOC, ticker)

    @staticmethod
    async def yahoo(symbol):
        return {"sector": "Technology", "industry": "Consumer Electronics"} if symbol == "AAPL" \
            else {"sector": "Energy", "industry": "Oil & Gas Refining & Marketing"}


def test_server_us_and_cache():
    FakeGeo.calls = []
    cache = CacheStore(connect(":memory:"))
    out = asyncio.run(server.geo_sector_logic("US", "AAPL", "Apple Inc.", 3571, cache=cache, src=FakeGeo()))
    assert out["geography"]["source"] == "sec_10k" and out["geography"]["fiscal_period_end"] == "2025-09-27"
    assert out["sector"]["sector"] == "Information Technology"
    again = asyncio.run(server.geo_sector_logic("US", "AAPL", "Apple Inc.", 3571, cache=cache, src=FakeGeo()))
    assert FakeGeo.calls == ["AAPL"] and "from_cache" in again["flags"]


def test_server_india_and_failure_default():
    out = asyncio.run(server.geo_sector_logic("IN", "RELIANCE", "Reliance Industries Limited", cache=CacheStore(connect(":memory:")),
                                              src=FakeGeo()))
    assert out["geography"]["source"] == "trendlyne_annual_report" and out["sector"]["sector"] == "Energy"

    class Broken(FakeGeo):
        @staticmethod
        async def india(ticker, name):
            raise RuntimeError("Trendlyne down")

    out = asyncio.run(server.geo_sector_logic("IN", "RELIANCE", "Reliance", cache=CacheStore(connect(":memory:")), src=Broken()))
    assert out["geography"]["weights"] == {"IN": 1.0} and "geography_home_default" in out["flags"]


def test_node_records_and_sector():
    result = asyncio.run(server.geo_sector_logic("US", "AAPL", "Apple Inc.", 3571, cache=CacheStore(connect(":memory:")),
                                                 src=FakeGeo()))

    async def fetch(market, ticker, name, sic):
        return result

    out = asyncio.run(make_geo_sector_node(fetch)({"ticker": "AAPL", "market": "US", "company_name": "Apple Inc.",
                                                    "sic_code": 3571}))
    assert out["sector"] == "Information Technology" and out["geo_exposure"]["reliable"] is True
    ids = {r["record_id"] for r in build_records(result, "AAPL")}
    assert "sec_10k:AAPL:revenue_share_CN:FY2025" in ids and "sic:AAPL:sector" in ids
