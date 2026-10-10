"""
mcp_servers/macro_data/server.py: macro_data_server, the MCP server behind the Macro agent.

What this file does:
    Offers one tool, get_macro_panel(), which returns the economic picture for the six
    panel economies (US, India, China, Eurozone, Japan, UK) with regime labels (design
    14.5). The panel is the same for every company, so it is built once a day and cached
    for 24 hours (MACRO_CACHE_TTL_HOURS): about 16 outside calls the first time, none
    after that.

      US        FRED: policy rate, 10-year yield, yield curve, inflation, GDP growth,
                industrial production, unemployment, the dollar, credit spread
      others    BIS policy rates (one call for all five), IMF growth and inflation (one
                call each for all countries), the World Bank if the IMF fails
      India     also FRED's rupee per dollar rate

    Every indicator keeps its date and source; anything older than the limit in
    regime_rules.yaml is flagged stale, and anything taken from a fallback is flagged.

Run it by itself (for debugging) with:
    uv run python -m invest_agent.mcp_servers.macro_data.server
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from invest_agent.cache.store import CacheStore
from invest_agent.common.logging_setup import setup_logging
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.mcp_servers.macro_data import bis, fred, imf, regime_rules, worldbank

logger = logging.getLogger(__name__)

mcp = FastMCP("macro_data_server")
CACHE_NAMESPACE = "macro_panel"


class Sources:
    """The outside calls, as attributes so tests can replace them."""
    fred_fetch = staticmethod(fred.fetch)
    bis_rates = staticmethod(bis.policy_rates)
    imf_indicator = staticmethod(imf.indicator)
    wb_latest = staticmethod(worldbank.latest)


async def _fred_indicators(market: str, src: Sources, flags: list[str]) -> dict:
    out: dict = {}
    for name, spec in (load_yaml("mcp_servers/macro_data/series_map.yaml").get(market) or {}).items():
        try:
            rows = await src.fred_fetch(spec["id"], spec.get("frequency"), 30)
        except Exception as err:
            logger.warning("FRED %s failed: %s", spec["id"], err)
            flags.append(f"{market}_{name}_unavailable")
            continue
        if not rows:
            continue
        transform = fred.TRANSFORMS[spec["transform"]]
        value = transform(rows) if spec["transform"] != "level" else transform(rows, 6)
        if name == "credit_spread":
            value.update({k: v for k, v in fred.level(rows, 3).items() if k.startswith("change")})
        if value:
            out[name] = {**value, "source": "fred", "series": spec["id"]}
    return out


async def panel_logic(today: date | None = None, cache: CacheStore | None = None, src: Sources | None = None) -> dict:
    """The work behind get_macro_panel, separate so tests can call it with fakes."""
    today = today or date.today()
    cache = cache or CacheStore()
    if (hit := cache.get(CACHE_NAMESPACE, "panel")) is not None:
        return {**hit, "flags": hit.get("flags", []) + ["from_cache"]}

    src = src or Sources()
    panel = load_yaml("mcp_servers/macro_data/panel.yaml")
    rules = load_yaml("mcp_servers/macro_data/regime_rules.yaml")
    flags: list[str] = []
    countries: dict[str, dict] = {code: {"name": c["name"], "indicators": {}} for code, c in panel["countries"].items()}

    # US and India's rupee from FRED.
    countries["US"]["indicators"].update(await _fred_indicators("US", src, flags))
    countries["IN"]["indicators"].update(await _fred_indicators("IN", src, flags))

    # Policy rates for everyone else from the BIS, in one call.
    bis_codes = {c["bis"]: code for code, c in panel["countries"].items() if code != "US"}
    try:
        rates = await src.bis_rates(list(bis_codes))
        for bis_code, rows in rates.items():
            if bis_code in bis_codes and rows:
                countries[bis_codes[bis_code]]["indicators"]["policy_rate"] = {
                    **bis.latest_and_change(rows, rules["rates"]["lookback_months"]), "source": "bis"}
    except Exception as err:
        logger.warning("BIS failed: %s", err)
        flags.append("bis_unavailable")

    # Growth and inflation for everyone but the US from the IMF; the World Bank if it fails.
    for name, imf_code in panel["imf_indicators"].items():
        try:
            data = await src.imf_indicator(imf_code)
        except Exception as err:
            logger.warning("IMF %s failed: %s", imf_code, err)
            flags.append(f"imf_{name}_unavailable")
            data = None
        for code, c in panel["countries"].items():
            if code == "US":
                continue
            value = imf.this_and_last_year(data.get(c["imf"], {}), today.year) if data else {}
            if value:
                countries[code]["indicators"][name] = {**value, "source": "imf", "series": imf_code}
                continue
            try:
                value = await src.wb_latest(c["world_bank"], panel["world_bank_indicators"][name])
            except Exception as err:
                logger.warning("World Bank %s %s failed: %s", code, name, err)
                value = {}
            if value:
                countries[code]["indicators"][name] = {**value, "source": "world_bank"}
                flags.append(f"{code}_{name}_from_world_bank")

    # Freshness, then the regime labels.
    for code, c in countries.items():
        for name, ind in c["indicators"].items():
            days = rules["stale_after_days_quarterly"] if "recent_two_quarters" in ind else rules["stale_after_days"]
            d = str(ind.get("date", ""))
            if len(d) == 10 and d < (today - timedelta(days=days)).isoformat():
                ind["stale"] = True
        c["regimes"] = regime_rules.label_country(code, c["indicators"])

    result = {"as_of": today.isoformat(), "countries": countries, "flags": flags}
    if any(c["indicators"] for c in countries.values()):
        cache.set(CACHE_NAMESPACE, "panel", result, ttl=timedelta(hours=get_settings().macro_cache_ttl_hours))
    return result


@mcp.tool()
async def get_macro_panel() -> dict:
    """The six panel economies (US, IN, CN, EA, JP, GB): each with its indicators (value,
    date, source, changes) and regime labels (rates, inflation, growth, credit, yield
    curve). Returns {as_of, countries: {code: {name, indicators, regimes}}, flags}."""
    return await panel_logic()


if __name__ == "__main__":
    setup_logging()
    mcp.run()
