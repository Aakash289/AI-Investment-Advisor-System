"""
mcp_servers/news/server.py: news_server, the MCP server behind the News agent.

What this file does:
    Offers one tool, get_news(market, ticker, company_name, cik), which gathers
    everything the News agent labels and scores (design 14.6):

      articles      Tavily, 2 searches on trusted outlets for the market (2 credits)
      disclosures   US: 8-K filings from the last 90 days with their item codes (SEC)
                    India: Trendlyne's news and exchange announcements, and insider and
                    promoter trades from the last 6 months (2 Trendlyne calls)
      coverage      GDELT's 90-day volume and tone, when it can be reached (optional, A7)

    Articles and Indian disclosures are deduplicated and given ids for the labelling
    call; 8-Ks are read by plain code and passed on separately. The answer is cached for
    an hour (NEWS_CACHE_TTL_MINUTES), so asking about the same company again within the
    hour uses no Tavily credits or Trendlyne calls.

Run it by itself (for debugging) with:
    uv run python -m invest_agent.mcp_servers.news.server
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from invest_agent.cache.store import CacheStore
from invest_agent.common.logging_setup import setup_logging
from invest_agent.config import get_settings
from invest_agent.mcp_servers.news import (
    aggregate,
    gdelt,
    sec_8k,
    spike,
    tavily,
    trendlyne_disclosures,
)
from invest_agent.storage.db import connect, get_quota_units

logger = logging.getLogger(__name__)

mcp = FastMCP("news_server")
CACHE_NAMESPACE = "news"
LEGAL_WORDS = re.compile(r"\b(limited|ltd\.?|inc\.?|incorporated|corporation|corp\.?|plc|co\.?)$", re.I)


def search_name(company_name: str) -> str:
    """Drop legal words, which make searches worse: Reliance Industries Limited -> Reliance Industries."""
    name = company_name.strip().rstrip(".,")
    while LEGAL_WORDS.search(name):
        name = LEGAL_WORDS.sub("", name).strip().rstrip(".,")
    return name


def credits_left() -> int:
    used = get_quota_units(connect(), "tavily", "month")
    return max(get_settings().tavily_monthly_credit_limit - used, 0)


class Sources:
    """The outside calls, as attributes so tests can replace them."""
    articles = staticmethod(tavily.company_articles)
    indian_disclosures = staticmethod(trendlyne_disclosures.disclosures)
    us_filings = staticmethod(sec_8k.recent_8ks)
    coverage = staticmethod(gdelt.coverage)
    credits = staticmethod(credits_left)


async def news_logic(market: str, ticker: str, company_name: str, cik: str | None = None,
                     cache: CacheStore | None = None, src: Sources | None = None) -> dict:
    cache = cache or CacheStore()
    key = f"{market}:{ticker}"
    if (hit := cache.get(CACHE_NAMESPACE, key)) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}

    src = src or Sources()
    name = search_name(company_name or ticker)
    flags: list[str] = []
    items: list[dict] = []
    filings: list[dict] = []

    # The sources do not depend on each other, so they are asked at the same time (speed,
    # 7 October: one after another they took about 20 of the news step's 48 seconds).
    async def tavily_part():
        try:
            return await src.articles(name, market, src.credits())
        except Exception as err:
            logger.warning("Tavily failed for %s: %s", name, err)
            return [], ["tavily_failed"], 0

    async def disclosures_part():
        if market == "IN":
            found, tl_flags = await src.indian_disclosures(ticker)
            return found, tl_flags, []
        if cik:
            try:
                return [], [], await src.us_filings(cik)
            except Exception as err:
                logger.warning("SEC 8-Ks failed for %s: %s", ticker, err)
                return [], ["sec_8k_unavailable"], []
        return [], [], []

    (found, tav_flags, used), (disc, disc_flags, filings), (coverage, cov_flags) = await asyncio.gather(
        tavily_part(), disclosures_part(), src.coverage(name))
    items += found + disc
    flags += tav_flags + disc_flags + cov_flags

    result = {
        "market": market, "ticker": ticker, "search_name": name,
        "items": aggregate.dedupe(items), "filings": filings,
        "serious_filings": spike.serious_filings(filings),
        "coverage": coverage, "spike": spike.coverage_spike(coverage.get("volume", [])),
        "tavily_credits_used": used, "flags": flags, "as_of": date.today().isoformat(),
    }
    if result["items"] or filings:
        cache.set(CACHE_NAMESPACE, key, result, ttl=timedelta(minutes=get_settings().news_cache_ttl_minutes))
    return result


@mcp.tool()
async def get_news(market: str, ticker: str, company_name: str, cik: str | None = None) -> dict:
    """Recent articles and official disclosures about one company, ready for labelling.

    market: "US" or "IN"; ticker: "AAPL" or "RELIANCE"; company_name: the full name;
    cik: SEC company number (US only, for 8-K filings).
    Returns {items: [{id, title, snippet, date, url, outlet, origin, trusted}], filings
    (US 8-Ks), serious_filings, coverage (GDELT volume and tone, when available), spike,
    tavily_credits_used, flags}.
    """
    return await news_logic(market, ticker, company_name, cik)


if __name__ == "__main__":
    setup_logging()
    mcp.run()
