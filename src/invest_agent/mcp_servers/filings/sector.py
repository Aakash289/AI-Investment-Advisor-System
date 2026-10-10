"""
mcp_servers/filings/sector.py: places a company in one of our 11 sectors (design 14.7).

What this file does:
    US companies: the SIC code from SEC (Apple 3571, computers) mapped through the
    ranges in sector_taxonomy.yaml. Yahoo's sector is mapped too, and if the two differ
    the SIC code wins and the difference is flagged (Yahoo's label is the one people
    usually see, so the reader should know).
    Indian companies: Yahoo's sector, mapped the same way (Reliance: Energy).

    The sector feeds the sector sensitivity table in the macro fit score (step 9).
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml


def from_sic(sic: int | None) -> str | None:
    if not sic:
        return None
    for low, high, sector in load_yaml("mcp_servers/filings/sector_taxonomy.yaml")["sic"]:
        if low <= int(sic) <= high:
            return sector
    return None


def from_yahoo(label: str | None) -> str | None:
    return load_yaml("mcp_servers/filings/sector_taxonomy.yaml")["yahoo"].get(label or "")


def classify(market: str, sic: int | None, yahoo_sector: str | None, yahoo_industry: str | None) -> dict:
    by_sic, by_yahoo = from_sic(sic), from_yahoo(yahoo_sector)
    out = {"sector": None, "source": None, "sic": sic, "yahoo_sector": yahoo_sector,
           "yahoo_industry": yahoo_industry, "flags": []}
    if market == "US" and by_sic:
        out.update(sector=by_sic, source="sic")
        if by_yahoo and by_yahoo != by_sic:
            out["flags"].append(f"sector_disagreement: SIC says {by_sic}, Yahoo says {by_yahoo}")
    elif by_yahoo:
        out.update(sector=by_yahoo, source="yahoo")
    else:
        out["flags"].append("sector_unknown")
    return out
