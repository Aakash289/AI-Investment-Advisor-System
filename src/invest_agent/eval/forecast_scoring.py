"""
eval/forecast_scoring.py: how close were the year-1 projections? (design 14.10, 14.17)

What this file does:
    Every analysis saves its year-1 projection in the decision log: revenue and net
    profit for the next fiscal year, for the low, base and high cases. Once that year's
    annual report is out, this file compares:

      error          (base case - actual) / actual, for revenue and for net profit;
                     +5% means the base case was 5% too high
      inside range   whether the actual landed between the low and high cases

    summarise() then gives the average size of the errors and the share of actuals
    inside the range. The range is meant to be wide enough to hold most outcomes: if
    actuals fall outside it much more often than about a third of the time (more than
    45%, with at least 10 cases; backtest_config.yaml), the summary says the range is too
    narrow, which points at the one-standard-deviation width in forecast_config.yaml.

    This needs no historical data source: the projections were saved when they were made.
"""

from __future__ import annotations

from statistics import mean

from invest_agent.eval.scoring import config


def _one(cases: dict, field: str, actual: float | None) -> dict | None:
    base = (cases.get("base") or {}).get(field)
    if actual is None or base is None or actual == 0:
        return None
    lows = [(cases.get(c) or {}).get(field) for c in ("low", "high")]
    lo, hi = (min(lows), max(lows)) if None not in lows else (base, base)
    return {"projected": base, "actual": actual, "error": round((base - actual) / abs(actual), 6),
            "low": lo, "high": hi, "inside": lo <= actual <= hi}


def score_projection(year1: dict, actual_revenue: float | None, actual_profit: float | None) -> dict:
    """year1 is the decision log's "year1" block; actuals are in the same currency and units."""
    cases = year1.get("cases") or {}
    return {"fiscal_year": year1.get("fiscal_year"),
            "revenue": _one(cases, "revenue", actual_revenue),
            "profit": _one(cases, "net_income", actual_profit)}


def summarise(scored: list[dict]) -> dict:
    cfg = config()
    out = {"count": len(scored)}
    for field in ("revenue", "profit"):
        rows = [s[field] for s in scored if s.get(field)]
        inside = [1.0 if r["inside"] else 0.0 for r in rows]
        outside_share = 1 - mean(inside) if inside else None
        out[field] = {
            "count": len(rows),
            "average_error": round(mean(r["error"] for r in rows), 6) if rows else None,
            "average_size_of_error": round(mean(abs(r["error"]) for r in rows), 6) if rows else None,
            "inside_range": round(mean(inside), 4) if inside else None,
            "range_too_narrow": (outside_share > cfg["forecast_outside_flag"]) if len(rows) >= cfg["min_forecasts"]
            else None,
        }
    return out
