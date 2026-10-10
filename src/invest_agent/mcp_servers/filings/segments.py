"""
mcp_servers/filings/segments.py: US revenue by geography from the latest 10-K (design 14.7).

What this file does:
    SEC's company facts file leaves out every figure broken down by a dimension, and
    revenue by region is exactly such a figure, so the full XBRL of the latest 10-K is
    read instead, with the edgartools library (free, MIT licence; install with
    uv add edgartools).

    The probe on 6 October (scripts/geo_probe.py) showed what Apple's FY2025 10-K holds:
      StatementGeographicalAxis    country:US $151.8 billion, country:CN $64.4 billion,
                                   other countries $200.0 billion
      StatementBusinessSegmentsAxis Americas, Europe, Greater China, Japan, Rest of Asia
                                   Pacific (Apple's segments are regions)
    Both are returned as candidate breakdowns; exposure_checks.choose() picks one. The
    undimensioned revenue for the same year is the total the parts are checked against.

    The download is one filing per company and the result is cached for 7 days by the
    server, since segments change once a year.
"""

from __future__ import annotations

from datetime import date

from invest_agent.config import get_settings

REVENUE_CONCEPTS = ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues",
                    "us-gaap:SalesRevenueNet")
AXES = ("dim_srt_StatementGeographicalAxis", "dim_us-gaap_StatementGeographicalAxis",
        "dim_us-gaap_StatementBusinessSegmentsAxis")
# Segment figures also carry ConsolidationItemsAxis = OperatingSegmentsMember, which only
# says "this is a segment figure, before eliminations"; it does not split revenue again,
# so it is ignored when deciding whether a row has a single breakdown (found in the first
# live run on 6 October, when Apple's regional segments were skipped because of it).
COMPANION = {"dim_srt_ConsolidationItemsAxis": "us-gaap:OperatingSegmentsMember"}


def _is_year(start, end) -> bool:
    try:
        days = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days
    except ValueError:
        return False
    return 350 <= days <= 380


def breakdowns(rows: list[dict]) -> dict:
    """From the filing's fact rows (dicts with concept, numeric_value, period_start,
    period_end and dim_* columns), the latest full-year revenue breakdown for each axis.

    Returns {"period_end", "total", "candidates": [{axis, parts: [{name, value}],
    reported_total}]}. A row counts for an axis only if that is its only dimension, so
    a figure split two ways at once (a region's product line) is not double counted.
    """
    revenue = [r for r in rows if r.get("concept") in REVENUE_CONCEPTS and r.get("numeric_value") is not None
               and _is_year(r.get("period_start"), r.get("period_end"))]
    if not revenue:
        return {"period_end": None, "total": None, "candidates": []}
    period_end = max(str(r["period_end"])[:10] for r in revenue)
    latest = [r for r in revenue if str(r["period_end"])[:10] == period_end]

    def dims(r):
        return [k for k, v in r.items() if k.startswith("dim_") and v not in (None, "") and v == v
                and COMPANION.get(k) != v]

    total = next((float(r["numeric_value"]) for r in latest if not dims(r)), None)
    candidates = []
    for axis in AXES:
        parts = [{"name": str(r[axis]), "value": float(r["numeric_value"])}
                 for r in latest if dims(r) == [axis]]
        if parts:
            candidates.append({"axis": axis.split("_")[-1], "parts": parts, "reported_total": total})
    return {"period_end": period_end, "total": total, "candidates": candidates}


def latest_10k_rows(ticker: str) -> dict:
    """Downloads the latest 10-K's XBRL with edgartools. Ordinary (not async) code:
    the server runs it in a worker thread through tracked_call."""
    from edgar import Company, set_identity        # imported here so the rest works without it

    set_identity(get_settings().sec_edgar_user_agent)
    filing = Company(ticker).get_filings(form="10-K").latest()
    frame = filing.xbrl().facts.to_dataframe()
    return {"accession": filing.accession_no, "filed": str(filing.filing_date),
            "rows": frame.to_dict(orient="records")}
