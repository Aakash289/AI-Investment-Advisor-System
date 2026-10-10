"""
mcp_servers/filings/server.py: filings_server, the MCP server behind the Fundamentals Agent.

What this file does:
    Offers three tools. get_fundamentals(market, ticker, cik, sic) returns six years of a
    company's annual figures, the measures built from them, whether bank mode applies,
    and where everything came from (design 14.3):

      US companies     SEC EDGAR company facts (Tier 1, free). If SEC fails, or is
                       missing revenue or net income for at least 3 years, FMP's free
                       plan instead (Tier 2), flagged as a fallback.
      Indian companies Trendlyne MCP Pro (Tier 2): six years of figures, consolidated,
                       in rupees; plus capex from yfinance's annual cash flow (Tier 2,
                       flagged), since Trendlyne has no capex history.

    Bank mode (banks, NBFCs, insurers): US SIC codes 6000 to 6411 except 6798 (REITs),
    or a Trendlyne industry containing Bank, NBFC, Finance or Insurance (bank_mode.yaml).

    get_geo_sector(market, ticker, company_name, sic) returns the share of revenue from
    each panel economy (US: the latest 10-K read with edgartools; India: the annual
    report's within and outside India table via Trendlyne) and the company's sector
    (design 14.7, build step 8c), cached for 7 days.

    get_peer_multiples(market, ticker, industry) returns P/E, P/B, EV/EBITDA (and P/S
    for US) for the company's peers, for the valuation step (design 14.9, build step 7b):
    US peers from FMP's free list with SEC frames for their figures (peers_us.py);
    Indian peers from Trendlyne's industry list and values (peers_india.py).

    Answers are cached for 24 hours (FUNDAMENTALS_CACHE_TTL_HOURS), so asking about the
    same company again costs no SEC, FMP or Trendlyne calls.

Run it by itself (for debugging) with:
    uv run python -m invest_agent.mcp_servers.filings.server
"""

from __future__ import annotations

import logging
from datetime import timedelta

from mcp.server.fastmcp import FastMCP

from invest_agent.cache.store import CacheStore
from invest_agent.common.logging_setup import setup_logging
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.mcp_servers.filings import (
    exposure_checks,
    fmp,
    peers_india,
    peers_us,
    sec_edgar,
    sector,
    segments,
    trendlyne,
    trendlyne_segments,
    yahoo_cashflow,
)
from invest_agent.mcp_servers.filings.metrics import compute_metrics
from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts
from invest_agent.observability.tracked_call import tracked_call

logger = logging.getLogger(__name__)

mcp = FastMCP("filings_server")
CACHE_NAMESPACE = "fundamentals"


# --- Rules shared by both markets ----------------------------------------------------------

def is_bank(market: str, sic: int | None, industry: str | None) -> bool:
    rules = load_yaml("verification/bank_mode.yaml")["detect"]
    if market == "US" and sic:
        low, high = rules["us_sic_range"]
        return low <= int(sic) <= high and int(sic) not in rules["us_sic_excluded"]
    if market == "IN" and industry:
        return any(word.lower() in industry.lower() for word in rules["india_industries"])
    return False


def has_enough(figures: dict) -> bool:
    """The critical fields of design 14.9: at least 3 years of revenue and net income."""
    return len(figures.get("revenue", [])) >= 3 and len(figures.get("net_income", [])) >= 3


def settle_indian_shares(figures: dict, flags: list[str]) -> None:
    """Trendlyne's share count has no stated unit. Check it against net income / EPS
    (which must be close to the share count) and keep whichever reading agrees; if none
    does, use the implied count and flag it."""
    shares = figures.get("shares_outstanding")
    ni, eps = figures.get("net_income"), figures.get("eps_basic")
    if not (ni and eps and eps[0]["value"]):
        return
    implied = ni[0]["value"] / eps[0]["value"]
    if shares:
        raw = shares[0]["value"]
        for scale in (1, 1e5, 1e7):                   # as is, in lakh, in crore
            if implied and abs(raw * scale / implied - 1) < 0.25:
                shares[0]["value"] = raw * scale
                return
    figures["shares_outstanding"] = [{**ni[0], "value": implied, "unit": "number",
                                      "tag": "derived:net_income/eps_basic"}]
    flags.append("shares_derived_from_eps")


async def settle_us_shares(figures: dict, ticker: str, flags: list[str]) -> None:
    """A US company with no share count in its SEC file (several share classes, as for
    Alphabet; found 7 October) gets one from net income / diluted EPS, or failing that from
    Yahoo's implied count (all classes, market value / price). Without it there is no market
    value, so no P/E and no valuation."""
    if figures.get("shares_outstanding"):
        return
    ni, eps = figures.get("net_income"), figures.get("eps_diluted")
    if ni and eps and eps[0]["value"] and ni[0]["fiscal_year"] == eps[0]["fiscal_year"]:
        figures["shares_outstanding"] = [{**ni[0], "value": ni[0]["value"] / eps[0]["value"], "unit": "shares",
                                          "tag": "derived:net_income/eps_diluted"}]
        flags.append("shares_derived_from_eps")
        return
    try:
        implied = await tracked_call("yfinance", "implied_shares", yahoo_cashflow.fetch_implied_shares, ticker,
                                     timeout=20)
    except Exception as err:
        logger.warning("Yahoo share count failed for %s: %s", ticker, err)
        implied = None
    if implied:
        latest = (figures.get("net_income") or [{}])[0]
        figures["shares_outstanding"] = [{"fiscal_year": latest.get("fiscal_year"), "period_end": latest.get("period_end"),
                                          "value": implied, "unit": "shares", "tag": "yfinance:impliedSharesOutstanding",
                                          "accession": None, "filed": None}]
        flags.append("shares_from_yfinance")


# --- The two markets ------------------------------------------------------------------------

async def us_fundamentals(ticker: str, cik: str | None) -> dict:
    flags: list[str] = []
    figures: dict = {}
    if cik:
        try:
            facts = await tracked_call("sec_edgar", "company_facts", sec_edgar.fetch_company_facts, cik, timeout=60)
            figures = normalise_company_facts(facts)
        except Exception as err:
            logger.warning("SEC company facts failed for %s: %s", ticker, err)
            flags.append("sec_failed")
    if has_enough(figures):
        await settle_us_shares(figures, ticker, flags)
        if any(r.get("tag", "").startswith("rebuilt:") for r in figures.get("operating_income", [])):
            flags.append("operating_income_rebuilt")
        return {"source": "sec_edgar", "tier": 1, "figures": figures, "flags": flags}

    flags.append("sec_incomplete" if figures else "sec_unavailable")
    try:
        figures = await tracked_call("fmp", "statements", fmp.fetch_statements, ticker, quota_units=3, timeout=30)
        return {"source": "fmp", "tier": 2, "figures": figures, "flags": flags + ["used_fallback"]}
    except Exception as err:
        logger.warning("FMP failed for %s: %s", ticker, err)
        return {"source": None, "tier": None, "figures": figures, "flags": flags + ["fmp_failed"]}


async def indian_fundamentals(ticker: str, company_name: str | None = None) -> dict:
    try:
        tl = await trendlyne.fetch_indian_fundamentals(ticker, company_name)
    except Exception as err:
        logger.warning("Trendlyne failed for %s: %s", ticker, err)
        return {"source": None, "tier": None, "figures": {}, "flags": ["trendlyne_failed"]}

    figures, flags = tl["figures"], list(tl["flags"])
    try:
        capex = await tracked_call("yfinance", "cashflow", yahoo_cashflow.fetch_capex, f"{ticker}.NS", timeout=20)
        if capex:
            figures["capex"] = capex
            flags.append("capex_from_yfinance")
    except Exception as err:
        logger.warning("yfinance capex failed for %s: %s", ticker, err)
        flags.append("capex_unavailable")
    settle_indian_shares(figures, flags)
    return {"source": "trendlyne", "tier": 2, "figures": figures, "flags": flags,
            "sector": tl.get("sector"), "industry": tl.get("industry")}


async def fundamentals_logic(market: str, ticker: str, cik: str | None = None, sic: int | None = None,
                             cache: CacheStore | None = None, company_name: str | None = None) -> dict:
    """The tool's work, separate from the MCP wrapper so tests can call it directly."""
    cache = cache or CacheStore()
    key = f"{market}:{ticker}"
    if (hit := cache.get(CACHE_NAMESPACE, key)) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}

    result = await (us_fundamentals(ticker, cik) if market == "US" else indian_fundamentals(ticker, company_name))
    bank = is_bank(market, sic, result.get("industry"))
    result.update({
        "market": market, "ticker": ticker, "bank_mode": bank,
        "metrics": compute_metrics(result["figures"], bank_mode=bank),
        "currency": "USD" if market == "US" else "INR",
    })
    if result["figures"]:
        hours = get_settings().fundamentals_cache_ttl_hours
        cache.set(CACHE_NAMESPACE, key, result, ttl=timedelta(hours=hours))
    return result


@mcp.tool()
async def get_fundamentals(market: str, ticker: str, cik: str | None = None, sic: int | None = None,
                           company_name: str | None = None) -> dict:
    """Six years of annual figures and the measures built from them, for one company.

    market: "US" or "IN". ticker: "AAPL" or the NSE symbol "RELIANCE".
    cik: SEC company number (US only). sic: SEC industry code (US only, for bank mode).
    Returns {source, tier, currency, figures: {name: [years]}, metrics: {name: [years]},
    bank_mode, sector, industry, flags}.
    """
    return await fundamentals_logic(market, ticker, cik, sic, company_name=company_name)


PEERS_NAMESPACE = "peers"


async def peers_logic(market: str, ticker: str, industry: str | None = None,
                      cache: CacheStore | None = None) -> dict:
    """The work behind get_peer_multiples, separate so tests can call it directly."""
    cache = cache or CacheStore()
    key = f"{market}:{ticker}"
    if (hit := cache.get(PEERS_NAMESPACE, key)) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}
    try:
        result = await (peers_us.us_peer_multiples(ticker) if market == "US"
                        else peers_india.india_peer_multiples(ticker, industry))
    except Exception as err:
        logger.warning("Peer multiples failed for %s: %s", ticker, err)
        return {"peers": [], "company": {}, "basis": None, "flags": ["peers_failed"]}
    result.update({"market": market, "ticker": ticker})
    if result.get("peers"):
        cache.set(PEERS_NAMESPACE, key, result, ttl=timedelta(hours=get_settings().fundamentals_cache_ttl_hours))
    return result


@mcp.tool()
async def get_peer_multiples(market: str, ticker: str, industry: str | None = None) -> dict:
    """P/E, P/B and EV/EBITDA (and P/S for US) for a company's peers.

    market: "US" or "IN". ticker: "AAPL" or the NSE symbol "RELIANCE".
    industry: Trendlyne's industry label (India only), e.g. "Refineries/Petro-Products".
    Returns {peers: [{symbol, name, market_value, pe, pb, ev_ebitda, ps}], company
    (India: the company's own values from the same source), basis, flags}.
    """
    return await peers_logic(market, ticker, industry)


GEO_NAMESPACE = "geo_sector"


def _yahoo_profile(symbol: str) -> dict:
    """Yahoo's sector and industry (yfinance; ordinary code, run in a thread)."""
    import yfinance as yf
    info = yf.Ticker(symbol).info or {}
    return {"sector": info.get("sector"), "industry": info.get("industry")}


class GeoSources:
    """The outside calls for get_geo_sector, as attributes so tests can replace them."""

    @staticmethod
    async def us_rows(ticker: str) -> dict:
        return await tracked_call("sec_edgar", "10k_xbrl", segments.latest_10k_rows, ticker, timeout=120, retries=1)

    @staticmethod
    async def india(ticker: str, company_name: str) -> dict | None:
        return await trendlyne_segments.india_breakdown(ticker, company_name)

    @staticmethod
    async def yahoo(symbol: str) -> dict:
        return await tracked_call("yfinance", "info", _yahoo_profile, symbol, timeout=20)


async def geo_sector_logic(market: str, ticker: str, company_name: str | None = None, sic: int | None = None,
                           cache: CacheStore | None = None, src: GeoSources | None = None) -> dict:
    """The work behind get_geo_sector, separate so tests can call it with fakes."""
    cache = cache or CacheStore()
    key = f"{market}:{ticker}"
    if (hit := cache.get(GEO_NAMESPACE, key)) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}
    src = src or GeoSources()
    flags: list[str] = []

    # Revenue by geography.
    geography, chosen = None, None
    try:
        if market == "US":
            filing = await src.us_rows(ticker)
            found = segments.breakdowns(filing["rows"])
            chosen = exposure_checks.choose(found["candidates"])
            if chosen:
                geography = {"weights": chosen["weights"], "source": "sec_10k", "axis": chosen["axis"],
                             "fiscal_period_end": found["period_end"], "accession": filing["accession"],
                             "parts": chosen["parts"], "sum_check": chosen["sum_check"], "reliable": True,
                             "unrecognised": chosen["unrecognised"]}
        else:
            found = await src.india(ticker, company_name or ticker)
            chosen = exposure_checks.choose([found]) if found else None
            if chosen:
                geography = {"weights": chosen["weights"], "source": "trendlyne_annual_report", "axis": chosen["axis"],
                             "parts": chosen["parts"], "sum_check": chosen["sum_check"], "reliable": True,
                             "unit": found.get("unit"), "unrecognised": chosen["unrecognised"]}
    except Exception as err:
        logger.warning("Geography failed for %s: %s", ticker, err)
        flags.append("geography_failed")
    if geography is None:
        geography = exposure_checks.home_default(market, "no usable revenue breakdown was found")
        flags.append("geography_home_default")
    elif geography.get("unrecognised"):
        flags.append("unrecognised_regions")

    # Sector.
    symbol = ticker if market == "US" else f"{ticker}.NS"
    try:
        yahoo = await src.yahoo(symbol)
    except Exception as err:
        logger.warning("Yahoo profile failed for %s: %s", symbol, err)
        yahoo = {}
    sector_info = sector.classify(market, sic, yahoo.get("sector"), yahoo.get("industry"))

    result = {"market": market, "ticker": ticker, "geography": geography, "sector": sector_info,
              "flags": flags + sector_info["flags"]}
    if geography.get("reliable") or sector_info.get("sector"):
        cache.set(GEO_NAMESPACE, key, result, ttl=timedelta(days=get_settings().segment_cache_ttl_days))
    return result


@mcp.tool()
async def get_geo_sector(market: str, ticker: str, company_name: str | None = None, sic: int | None = None) -> dict:
    """Revenue by panel economy and the company's sector.

    market: "US" or "IN"; ticker: "AAPL" or "RELIANCE"; company_name: full name (India,
    for the annual report search); sic: SEC industry code (US).
    Returns {geography: {weights: {US, IN, CN, EA, JP, GB, ROW: share}, source, reliable,
    sum_check, ...}, sector: {sector, source, sic, yahoo_sector, yahoo_industry}, flags}.
    """
    return await geo_sector_logic(market, ticker, company_name, sic)


if __name__ == "__main__":
    setup_logging()
    mcp.run()
