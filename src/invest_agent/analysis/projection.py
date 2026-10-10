"""
analysis/projection.py: the 2-year projection behind the fair value (design 14.10).

What this file does:
    From the verified annual figures it projects revenue, earnings, EPS, EBITDA and book
    value two years ahead, in three cases:

      case   revenue growth                                   margins (net and EBITDA)
      low    recent growth minus 1 standard deviation         lowest of the last 5 years
      base   recent growth: average of the last 3 usual years average of the last 3 years
      high   recent growth plus 1 standard deviation          highest of the last 5 years

    Base growth is the same measure business quality uses (usual_growth in
    fundamental_score.py): the trend line through the logarithm of revenue over the
    unbroken years, or, when one year is far from the rest, the average of the usual years. The standard deviation is that of
    the yearly growth rates, so a steady grower gets a narrow range and a volatile one a
    wide range. A year whose growth sits far from the others (more than 25 points from
    the middle) is left out of the spread and flagged, since one pandemic boom or
    acquisition would otherwise set the whole range (A13, 6 October); the trend still
    uses every year.

    Guardrails, each flagged when it bites:
      growth stays between -20% and +25% a year (forecast_config.yaml)
      margins stay inside the range the company has actually reached
      the share count stays at today's level, so buybacks are ignored (cautious)
      a year whose growth is far from the others is flagged ("one unusual year"), since
        the trend may then overstate what can repeat

    From revenue to what valuation needs:
      earnings = revenue x net margin; EPS = earnings / today's shares
      EBITDA = revenue x EBITDA margin
      book value = today's equity + year-1 earnings - last year's dividends

    Everything stays in the reporting currency. Nothing here is a reported figure: the
    caller tags each value "projected" with its method and assumptions.
"""

from __future__ import annotations

import math
import statistics

from invest_agent.analysis.fundamental_score import usual_growth
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.verification.record_index import RecordIndex

CASES = ("low", "base", "high")

COMMODITY_SECTORS = {"Energy", "Materials"}


def commodity_note(sector: str | None) -> str | None:
    if sector in COMMODITY_SECTORS:
        what = "oil and gas" if sector == "Energy" else "metals and other raw materials"
        return (f"Much of this company's revenue moves with the prices of {what}, which this projection cannot "
                f"forecast; read the revenue figures as a rough guide.")
    return None


def config() -> dict:
    return load_yaml("analysis/forecast_config.yaml")


def yearly_growth(points: list[tuple[int, float]]) -> list[float]:
    """Year-on-year growth rates for consecutive years, oldest first."""
    pts = sorted(points)
    return [b / a - 1 for (ya, a), (yb, b) in zip(pts, pts[1:]) if yb == ya + 1 and a > 0]


def _margin_series(idx: RecordIndex, numerator: str, years: list[int]) -> dict[int, float]:
    out = {}
    for y in years:
        top, rev = idx.value(numerator, y), idx.value("revenue", y)
        if top is not None and rev:
            out[y] = top / rev
    return out


def margin_outliers(series: dict[int, float], cfg: dict) -> dict[int, float]:
    """Years in the low-to-high window whose margin sits more than outlier_points from the
    window's median (a pandemic loss, a one-off gain); empty if fewer than 2 would remain."""
    years = sorted(series, reverse=True)[:cfg["range_years"]]
    limit = cfg.get("outlier_points")
    if not limit or len(years) < 3:
        return {}
    mid = statistics.median(series[y] for y in years)
    odd = {y: series[y] for y in years if abs(series[y] - mid) > limit}
    return odd if len(years) - len(odd) >= 2 else {}


def _cases(series: dict[int, float], cfg: dict) -> dict[str, float] | None:
    """low, base, high margins from a {year: margin} series (newest years first). The low
    and high cases leave out unusual margin years (margin_outliers)."""
    years = sorted(series, reverse=True)
    if not years:
        return None
    base = [series[y] for y in years[:cfg["base_years"]]]
    odd = margin_outliers(series, cfg)
    span = [series[y] for y in years[:cfg["range_years"]] if y not in odd]
    return {"low": min(span), "base": sum(base) / len(base), "high": max(span)}


def project(records: list[dict], *, unbroken_years: list[int] | None = None) -> dict:
    """Returns {"status", "flags", "assumptions", "years", "cases": {case: {year: figures}}}."""
    cfg = config()
    g_cfg, m_cfg = cfg["growth"], cfg["margins"]
    idx = RecordIndex(records)
    flags: list[str] = []

    years = sorted(unbroken_years or idx.years("revenue"))
    pts = [(y, idx.value("revenue", y)) for y in years if idx.value("revenue", y)]
    if len(pts) < g_cfg["min_years"]:
        return {"status": "not_enough_history", "flags": ["fewer_than_3_years"], "cases": {}}

    # A year far from the middle of the others (a pandemic boom, an acquisition, a
    # depressed starting year) is flagged and left out of both the base growth (A15) and
    # the spread (A13), so one odd year cannot set the projection on its own.
    ug = usual_growth(pts)
    rates = list(ug["rates"].values())
    usual_years = sorted(y for y in ug["rates"] if y not in ug["left_out"])
    usual = [ug["rates"][y] for y in usual_years]
    if ug["left_out"]:
        flags.append("one_unusual_year" if len(ug["left_out"]) == 1 else "unusual_years")
    sd = statistics.stdev(usual) if len(usual) >= 2 else 0.0
    # Base growth (decided 6 October, after Reliance): the average of the last 3 usual
    # years, the business as it is now; older rebounds (Reliance's FY2023 +24% after
    # COVID) stay in the spread but stop setting next year. The longer record is still
    # what the business quality score judges.
    recent_years = usual_years[-g_cfg["base_years"]:]
    recent = [ug["rates"][y] for y in recent_years]
    trend = math.exp(sum(math.log(1 + r) for r in recent) / len(recent)) - 1 if recent else ug["growth"]

    # The cap and floor apply to the base case first, and the low and high cases are then
    # built round the capped base (decided 7 October, Eternal: a 3-year trend of 65.7% was
    # capped at 25% in all three cases, so the range had no width). Growth beyond the cap
    # says little about next year, so the range is then kept at least min_swing_when_capped
    # wide on the side away from the cap.
    width = g_cfg["width_sd"] * sd
    base = max(g_cfg["floor"], min(g_cfg["cap"], trend))
    if base != trend:
        flags.append(f"growth_{'capped' if trend > base else 'floored'}_base")
        width = max(width, g_cfg.get("min_swing_when_capped", 0.0))
    growth = {"low": base - width, "base": base, "high": base + width}
    for case in ("low", "high"):
        g = growth[case]
        clipped = max(g_cfg["floor"], min(g_cfg["cap"], g))
        if clipped != g:
            flags.append(f"growth_{'capped' if g > clipped else 'floored'}_{case}")
        growth[case] = clipped

    all_years = idx.years("revenue")
    net_series = _margin_series(idx, "net_income", all_years)
    margin_left_out = margin_outliers(net_series, m_cfg)
    if margin_left_out:
        flags.append("unusual_margin_years")
    net_m = _cases(net_series, m_cfg)
    ebitda_m = _cases(_margin_series(idx, "ebitda", all_years), m_cfg)
    if net_m is None:
        return {"status": "no_margins", "flags": flags + ["no_net_income"], "cases": {}}

    last_year = pts[-1][0]
    revenue0 = pts[-1][1]
    # The latest year itself unusual (Eternal's FY2026 +161%, most likely a change in how
    # revenue is counted): the earlier years are a poor guide to next year, so the
    # analysis gives no fair value from this projection (decided 6 October).
    if last_year in ug["left_out"]:
        flags.append("latest_year_unusual")
    shares = idx.value("shares_outstanding")
    equity0 = idx.value("total_equity", last_year)
    dividends = idx.value("dividends_paid", last_year)
    if dividends is None and idx.value("dividend_per_share_1y") is not None and shares:
        dividends = idx.value("dividend_per_share_1y") * shares
    dividends = abs(dividends or 0.0)

    horizon = cfg["horizon_years"]
    out_cases: dict[str, dict[int, dict]] = {}
    for case in CASES:
        rows, equity = {}, equity0
        for step in range(1, horizon + 1):
            year = last_year + step
            revenue = revenue0 * (1 + growth[case]) ** step
            earnings = revenue * net_m[case]
            row = {"revenue": revenue, "net_income": earnings,
                   "eps": earnings / shares if shares else None,
                   "ebitda": revenue * ebitda_m[case] if ebitda_m else None}
            if equity is not None:
                equity = equity + earnings - dividends
                row["book_value"] = equity
                row["book_value_per_share"] = equity / shares if shares else None
            rows[year] = row
        out_cases[case] = rows

    if not shares:
        flags.append("no_share_count")
    return {
        "status": "ok", "flags": flags, "base_year": last_year, "years": [last_year + s for s in range(1, horizon + 1)],
        "assumptions": {
            "trend_growth": trend, "growth_method": "recent_years", "growth_cap": g_cfg["cap"],
            "growth_floor": g_cfg["floor"], "growth_min_swing": g_cfg.get("min_swing_when_capped", 0.0), "recent_years": [f"FY{y}" for y in recent_years],
            "long_run_growth": ug["growth"], "growth_sd": sd,
            "spread_years_left_out": len(rates) - len(usual),
            "left_out_years": {f"FY{y}": r for y, r in ug["left_out"].items()}, "growth": growth, "years_used": [y for y, _ in pts],
            "net_margin": net_m, "ebitda_margin": ebitda_m,
            "margin_left_out_years": {f"FY{y}": m for y, m in sorted(margin_left_out.items())}, "shares": shares, "dividends": dividends,
            "starting_revenue": revenue0, "starting_equity": equity0,
        },
        "cases": out_cases,
    }
