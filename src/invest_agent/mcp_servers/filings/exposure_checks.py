"""
mcp_servers/filings/exposure_checks.py: turns a reported revenue breakdown into panel weights (design 14.7).

What this file does:
    A company reports revenue by its own region names; the Macro agent describes six
    economies. This file:

      region_code()      maps a reported name to a panel code (US, IN, CN, EA, JP, GB) or
                         ROW, the rest of the world, using region_map.yaml. XBRL member
                         names are tidied first: "aapl:GreaterChinaSegmentMember" becomes
                         "greater china", "country:US" becomes "us"
      weights()          adds up each code's revenue and divides by the total, flagging
                         names it did not recognise
      sum_check()        the parts must add up to the total revenue within 1%; otherwise
                         the breakdown is marked unreliable (something is missing or
                         counted twice) and the home-country default is used instead
      choose()           when a filing has more than one breakdown (Apple reports both
                         countries and regional segments), the one that places more revenue
                         in named panel economies wins, since it says more
      home_default()     100% home country, flagged, when nothing usable is reported
"""

from __future__ import annotations

import re

from invest_agent.common.yaml_loader import load_yaml


def _cfg() -> dict:
    return load_yaml("mcp_servers/filings/region_map.yaml")


def tidy(name: str) -> str:
    """Turns an XBRL member into words: aapl:RestOfAsiaPacificSegmentMember -> rest of asia pacific."""
    text = str(name).split(":")[-1]
    text = re.sub(r"(Segment)?Member$", "", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)        # CamelCase to words
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def region_code(name: str) -> tuple[str, bool]:
    """(panel code or ROW, recognised?)"""
    code = _cfg()["regions"].get(tidy(name))
    return (code, True) if code else ("ROW", False)


def weights(parts: list[dict]) -> dict:
    """parts: [{name, value}]. Returns {weights: {code: share}, unrecognised: [...], total}."""
    total = sum(p["value"] for p in parts)
    sums: dict[str, float] = {}
    unknown = []
    for p in parts:
        code, known = region_code(p["name"])
        sums[code] = sums.get(code, 0.0) + p["value"]
        if not known:
            unknown.append(p["name"])
    shares = {code: round(v / total, 6) for code, v in sums.items()} if total > 0 else {}
    return {"weights": shares, "unrecognised": unknown, "total": total}


def sum_check(parts_total: float, reported_total: float | None) -> dict:
    tolerance = _cfg()["sum_check_tolerance"]
    if not reported_total:
        return {"status": "not_checked", "reason": "no total to compare with"}
    gap = abs(parts_total - reported_total) / abs(reported_total)
    return {"status": "ok" if gap <= tolerance else "failed", "gap": round(gap, 6)}


def named_share(w: dict) -> float:
    return sum(v for k, v in w.items() if k != "ROW")


def choose(candidates: list[dict]) -> dict | None:
    """candidates: [{axis, parts, total}]. The reliable breakdown that names the most."""
    best = None
    for c in candidates:
        result = {**c, **weights(c["parts"])}
        result["sum_check"] = sum_check(result["total"], c.get("reported_total"))
        if result["sum_check"]["status"] == "failed" or not result["weights"]:
            continue
        if best is None or named_share(result["weights"]) > named_share(best["weights"]):
            best = result
    return best


def home_default(market: str, reason: str) -> dict:
    code = _cfg()["home_default"].get(market, "ROW")
    return {"weights": {code: 1.0}, "source": "home_default", "reliable": False, "reason": reason}
