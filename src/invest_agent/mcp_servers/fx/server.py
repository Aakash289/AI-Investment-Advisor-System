"""
mcp_servers/fx/server.py: fx_server, the MCP server for exchange rates.

What this file does:
    Offers two tools (design 14.8):

      get_rate(base, quote)                   today's rate, quote per base, with its date,
                                              source, tier and any warning flags
      get_rate_series(base, quote, months)    one rate a month for the past months, for
                                              the currency risk note

    Source order for get_rate:
      1. Frankfurter pinned to FBIL, for pairs with the rupee, if FBIL's rate is no more
         than three business days old (FBIL is India's official rupee reference rate)
      2. Frankfurter's default rate, a blend of official central bank rates
      3. FRED DEXINUS (dollar and rupee only), flagged as lagged, since it runs about a
         week behind
      4. Yahoo Finance, flagged as unofficial
      If all four fail the answer has no rate and the flag fx_unavailable; nothing is
      ever converted at a guessed rate.

    Checks on the rate used: stale (older than three business days), and for the dollar
    and rupee, a cross-check of Frankfurter against FRED on FRED's latest date, flagged
    if they differ by more than 1%.

    Answers are cached for 24 hours (FX_CACHE_TTL_HOURS), shared by every request, so
    the system makes only a handful of FX calls a day.

Run it by itself (for debugging) with:
    uv run python -m invest_agent.mcp_servers.fx.server
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from invest_agent.cache.store import CacheStore
from invest_agent.common.logging_setup import setup_logging
from invest_agent.config import get_settings
from invest_agent.fx.checks import disagrees, is_stale, relative_gap
from invest_agent.mcp_servers.fx import fred_fx, frankfurter, yahoo_fx
from invest_agent.observability.tracked_call import tracked_call

logger = logging.getLogger(__name__)

mcp = FastMCP("fx_server")

RATE_NAMESPACE = "fx_rate"
SERIES_NAMESPACE = "fx_series"


async def _yahoo(base: str, quote: str) -> dict:
    # yahoo_fx.latest_rate is an ordinary function; tracked_call runs it in a thread.
    return await tracked_call("yfinance", "fx_rate", yahoo_fx.latest_rate, base, quote, timeout=20)


@dataclass
class Sources:
    """The four sources as plain functions, so tests can swap in fakes."""
    frankfurter_latest: Callable[..., Awaitable[dict]] = frankfurter.latest_rate   # (base, quote, provider)
    frankfurter_on: Callable[..., Awaitable[dict]] = frankfurter.rate_on       # (base, quote, day)
    fred: Callable[..., Awaitable[dict]] = fred_fx.latest_rate                 # (base, quote)
    yahoo: Callable[..., Awaitable[dict]] = _yahoo                             # (base, quote)


def _answer(base: str, quote: str, found: dict, source: str, provider: str, tier: int, flags: list) -> dict:
    return {"base": base, "quote": quote, "rate": found["rate"], "date": found["date"],
            "source": source, "provider": provider, "tier": tier, "flags": flags}


async def _pick_rate(base: str, quote: str, today: date, src: Sources, flags: list[str]) -> dict | None:
    """Try the sources in order; return the first usable answer, adding flags on the way."""
    if "INR" in (base, quote):
        try:
            found = await src.frankfurter_latest(base, quote, "FBIL")
            if not is_stale(found["date"], today):
                return _answer(base, quote, found, "frankfurter", "FBIL", 1, flags)
            flags.append("fbil_rate_behind")           # normal fallback, not a problem
        except Exception as err:
            logger.warning("Frankfurter (FBIL) failed for %s/%s: %s", base, quote, err)
            flags.append("fbil_unavailable")

    try:
        found = await src.frankfurter_latest(base, quote, None)
        return _answer(base, quote, found, "frankfurter", "official blend", 1, flags)
    except Exception as err:
        logger.warning("Frankfurter failed for %s/%s: %s", base, quote, err)
        flags.append("frankfurter_failed")

    if fred_fx.supports(base, quote):
        try:
            found = await src.fred(base, quote)
            flags.append("fx_lagged_fred_rate")
            return _answer(base, quote, found, "fred", "DEXINUS", 1, flags)
        except Exception as err:
            logger.warning("FRED failed for %s/%s: %s", base, quote, err)
            flags.append("fred_failed")

    try:
        found = await src.yahoo(base, quote)
        flags.append("fx_unofficial_rate")
        return _answer(base, quote, found, "yfinance", f"{base}{quote}=X", 2, flags)
    except Exception as err:
        logger.warning("Yahoo failed for %s/%s: %s", base, quote, err)
        flags.append("yfinance_failed")
    return None


async def _cross_check(answer: dict, src: Sources) -> dict:
    """Compare Frankfurter with FRED on FRED's latest date (dollar and rupee only)."""
    base, quote = answer["base"], answer["quote"]
    if answer["source"] != "frankfurter" or not fred_fx.supports(base, quote):
        return {"status": "not_applicable"}
    try:
        fred = await src.fred(base, quote)
    except fred_fx.FxSourceUnavailable as err:
        return {"status": "skipped", "reason": str(err)}
    except Exception as err:
        logger.warning("FRED cross-check failed: %s", err)
        return {"status": "unavailable"}
    try:
        same_day = await src.frankfurter_on(base, quote, fred["date"])
    except Exception as err:
        logger.warning("Frankfurter rate for %s failed: %s", fred["date"], err)
        return {"status": "unavailable"}
    gap = relative_gap(same_day["rate"], fred["rate"])
    return {"status": "disagree" if disagrees(same_day["rate"], fred["rate"]) else "agree",
            "date": fred["date"], "fred_rate": fred["rate"], "frankfurter_rate": same_day["rate"],
            "gap": round(gap, 6)}


async def rate_logic(base: str, quote: str, *, cache: CacheStore | None = None,
                     today: date | None = None, sources: Sources | None = None) -> dict:
    """The work behind get_rate, separate so tests can call it with fakes."""
    base, quote = base.upper(), quote.upper()
    today = today or date.today()
    if base == quote:
        return {"base": base, "quote": quote, "rate": 1.0, "date": today.isoformat(),
                "source": "same_currency", "provider": "none", "tier": 1, "flags": []}

    cache = cache or CacheStore()
    key = f"{base}:{quote}"
    if (hit := cache.get(RATE_NAMESPACE, key)) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}

    src = sources or Sources()
    flags: list[str] = []
    answer = await _pick_rate(base, quote, today, src, flags)
    if answer is None:
        # Not cached, so the next request tries the sources again.
        return {"base": base, "quote": quote, "rate": None, "date": None, "source": None,
                "provider": None, "tier": None, "flags": flags + ["fx_unavailable"]}

    if is_stale(answer["date"], today):
        flags.append("fx_rate_stale")
    answer["cross_check"] = await _cross_check(answer, src)
    if answer["cross_check"].get("status") == "disagree":
        flags.append("fx_sources_disagree")
    answer["flags"] = flags

    cache.set(RATE_NAMESPACE, key, answer, ttl=timedelta(hours=get_settings().fx_cache_ttl_hours))
    return answer


async def series_logic(base: str, quote: str, months: int = 12, *, cache: CacheStore | None = None,
                       today: date | None = None, fetch: Callable[..., Awaitable[list]] | None = None) -> dict:
    """The work behind get_rate_series: one rate a month, oldest first."""
    base, quote = base.upper(), quote.upper()
    today = today or date.today()
    cache = cache or CacheStore()
    key = f"{base}:{quote}:{months}m"
    if (hit := cache.get(SERIES_NAMESPACE, key)) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}

    start = today - timedelta(days=round(months * 365 / 12))
    try:
        points = await (fetch or frankfurter.series)(base, quote, start, today)
    except Exception as err:
        logger.warning("Frankfurter series failed for %s/%s: %s", base, quote, err)
        return {"base": base, "quote": quote, "points": [], "source": None, "flags": ["fx_series_unavailable"]}

    answer = {"base": base, "quote": quote, "points": points, "source": "frankfurter", "flags": []}
    cache.set(SERIES_NAMESPACE, key, answer, ttl=timedelta(hours=get_settings().fx_cache_ttl_hours))
    return answer


@mcp.tool()
async def get_rate(base: str, quote: str) -> dict:
    """Today's exchange rate: how many units of quote one unit of base buys.

    base, quote: currency codes, e.g. "USD", "INR". USD to INR 96.2 means 96.2 rupees
    per dollar. Returns {base, quote, rate, date, source, provider, tier, flags,
    cross_check}; rate is None (flag fx_unavailable) if every source failed.
    """
    return await rate_logic(base, quote)


@mcp.tool()
async def get_rate_series(base: str, quote: str, months: int = 12) -> dict:
    """One rate a month for the past `months` months, oldest first, from Frankfurter.
    Returns {base, quote, points: [{date, rate}], source, flags}."""
    return await series_logic(base, quote, months)


if __name__ == "__main__":
    setup_logging()
    mcp.run()
