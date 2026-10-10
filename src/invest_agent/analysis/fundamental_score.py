"""
analysis/fundamental_score.py: F1, business quality and valuation (design 14.18).

What this file does:
    Business quality (50% of the scorecard), from the verified annual figures, five parts:

      growth            trend revenue growth: a straight line through the logarithm of
                        revenue over the unbroken years (at least 3), which uses every year,
                        unlike a start-to-end growth rate that one unusual year can distort;
                        when one year is far from the rest, the average of the usual years
                        instead (usual_growth, A15)
      profitability     average ROIC over the last 3 years (ROE for banks and insurers);
                        the latest year alone when that is all there is (Indian companies)
      margin trend      operating margin, last 3 years' average against the 3 before.
                        Direction, not level: software runs 30% margins and retailers 3%
      balance sheet     interest coverage; a company with net cash scores +2, and net debt
                        to EBITDA stands in when interest expense is not reported
      cash generation   the share of years with positive free cash flow

    Banks and insurers skip balance sheet and cash generation (bank mode, 14.3).

    Valuation (20% of the scorecard), from the peer comparison in verification: the
    premium or discount to the peer median, P/E and EV/EBITDA averaged where both exist,
    P/B for banks and insurers. Cheap is not automatically good, which is why business
    quality carries twice the weight.

    Every part returns a finding with its display value and record IDs.
"""

from __future__ import annotations

import math
import statistics

from invest_agent.analysis.scoring_common import (
    band,
    combine,
    finding,
    pct,
    smooth_band,
    times,
)
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.verification.record_index import RecordIndex


def _cfg() -> dict:
    return load_yaml("analysis/scoring_bands.yaml")


def trend_growth(points: list[tuple[int, float]]) -> float | None:
    """Yearly growth from a least-squares line through log revenue. points: [(year, revenue)]."""
    pts = [(y, math.log(v)) for y, v in points if v and v > 0]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx, my = sum(y for y, _ in pts) / n, sum(v for _, v in pts) / n
    sxx = sum((y - mx) ** 2 for y, _ in pts)
    slope = sum((y - mx) * (v - my) for y, v in pts) / sxx
    return math.exp(slope) - 1


def usual_growth(points: list[tuple[int, float]]) -> dict | None:
    """The yearly growth rate the business normally shows (A15, 6 October).

    Normally the trend line through every year (trend_growth). But when one year's
    growth sits far from the others (more than jump_flag in forecast_config.yaml, 25
    points: a pandemic boom, a depressed starting year, an acquisition), the line is
    pulled up by that one year: Apple's FY2021 +33% turned a business growing about 3%
    a year into a 6.6% trend. Then the growth is the average of the usual years instead
    (their geometric mean), and the year left out is named.

    Returns {"growth", "method", "rates": {year: rate}, "left_out": {year: rate}}."""
    pts = sorted((y, v) for y, v in points if v and v > 0)
    if len(pts) < 3:
        return None
    rates = {yb: b / a - 1 for (ya, a), (yb, b) in zip(pts, pts[1:]) if yb == ya + 1}
    jump = load_yaml("analysis/forecast_config.yaml")["growth"]["jump_flag"]
    left_out: dict[int, float] = {}
    if len(rates) >= 3:
        middle = statistics.median(rates.values())
        left_out = {y: r for y, r in rates.items() if abs(r - middle) > jump}
        if len(rates) - len(left_out) < 2:
            left_out = {}
    if left_out:
        usual = [r for y, r in rates.items() if y not in left_out]
        growth = math.exp(sum(math.log(1 + r) for r in usual) / len(usual)) - 1
        return {"growth": growth, "method": "usual_years", "rates": rates, "left_out": left_out}
    return {"growth": trend_growth(pts), "method": "trend", "rates": rates, "left_out": {}}


def growth_text(g: dict, pts: list[tuple[int, float]]) -> str:
    span = f"over {len(pts)} years (FY{pts[0][0]} to FY{pts[-1][0]})"
    if g["method"] == "usual_years":
        odd = ", ".join(f"FY{y}'s {pct(r, signed=True)}" for y, r in g["left_out"].items())
        return f"Revenue has grown about {pct(g['growth'])} a year {span}, leaving out {odd} as unusual"
    return f"Revenue has grown about {pct(g['growth'])} a year on trend {span}"


def _ids(index: RecordIndex, field: str, years: list[int]) -> list[str]:
    return [r["record_id"] for r in index.by_field.get(field, []) if r.get("fiscal_year") in years]


def business_quality(records: list[dict], *, bank_mode: bool, unbroken_years: list[int] | None = None) -> dict:
    cfg = _cfg()["business_quality"]
    comps = cfg["components"]
    idx = RecordIndex(records)
    scores: dict[str, int | None] = {}
    findings: list[dict] = []

    # Growth: trend over the unbroken run of years.
    years = sorted(unbroken_years or idx.years("revenue"))
    pts = [(y, idx.value("revenue", y)) for y in years if idx.value("revenue", y)]
    ug = usual_growth(pts)
    if ug is not None and ug["growth"] is not None:
        g = ug["growth"]
        scores["growth"] = band(g, comps["growth"]["bands"])
        findings.append(finding("growth", scores["growth"], growth_text(ug, pts),
                                {"trend_growth": pct(g)}, _ids(idx, "revenue", [y for y, _ in pts])))

    # Profitability: average ROIC (ROE for banks) over the last 3 years. Trendlyne gives
    # debt and cash for the latest year only, so Indian companies have one year of ROIC;
    # that year is used on its own and the finding says so, rather than leaving it out.
    measure = "roe" if bank_mode else "roic"
    recent = idx.years(measure)[:3]
    if recent:
        avg = sum(idx.value(measure, y) for y in recent) / len(recent)
        scores["profitability"] = band(avg, comps["profitability"]["bands"])
        text = (f"{measure.upper()} averaged {pct(avg)} over the last {len(recent)} years" if len(recent) > 1 else
                f"{measure.upper()} was {pct(avg)} in FY{recent[0]} (only the latest year is available)")
        findings.append(finding("profitability", scores["profitability"], text,
                                {measure: pct(avg), "years": str(len(recent))}, _ids(idx, measure, recent)))

    # Margin trend: last 3 years' operating margin against the 3 before, in points.
    om_years = idx.years("operating_margin")
    if len(om_years) >= 4:
        last, before = om_years[:3], om_years[3:6]
        a = sum(idx.value("operating_margin", y) for y in last) / len(last)
        b = sum(idx.value("operating_margin", y) for y in before) / len(before)
        change = a - b
        long_score = band(change, comps["margin_trend"]["bands"])
        text = (f"Operating margin averaged {pct(a)} over the last {len(last)} years, against "
                f"{pct(b)} over the {len(before)} before ({change * 100:+.1f} points)")
        display = {"recent": pct(a), "earlier": pct(b), "change_points": f"{change * 100:+.1f}"}
        # The direction within the last 3 years too (Star Health: up against its COVID-loss
        # years, but down from 8.3% to 4.5% since). When the two point opposite ways, the
        # score is their average, rounded towards zero, and the finding says so.
        first, latest = idx.value("operating_margin", last[-1]), idx.value("operating_margin", last[0])
        recent_change = latest - first
        recent_score = band(recent_change, comps["margin_trend"]["bands"])
        if long_score * recent_score < 0:
            scores["margin_trend"] = int((long_score + recent_score) / 2)
            text += (f", but it moved from {pct(first)} in FY{last[-1]} to {pct(latest)} in FY{last[0]} "
                     f"({recent_change * 100:+.1f} points), so the two directions are averaged")
            display.update({"first": pct(first), "latest": pct(latest),
                            "recent_change_points": f"{recent_change * 100:+.1f}"})
        else:
            scores["margin_trend"] = long_score
        findings.append(finding("margin_trend", scores["margin_trend"], text, display,
                                _ids(idx, "operating_margin", last + before)))

    if not bank_mode:
        # Balance sheet: net cash scores +2, otherwise interest coverage.
        year = idx.latest_year()
        net_debt = idx.value("net_debt", year) if year else None
        cover = idx.value("interest_coverage", year) if year else None
        if net_debt is not None and net_debt < 0:
            scores["balance_sheet"] = 2
            findings.append(finding("balance_sheet", 2, f"Net cash in FY{year}: cash exceeds debt",
                                    {"net_debt": f"{net_debt:,.0f}"}, _ids(idx, "net_debt", [year])))
        elif cover is not None:
            scores["balance_sheet"] = band(cover, comps["balance_sheet"]["bands"])
            findings.append(finding("balance_sheet", scores["balance_sheet"],
                                    f"Operating income covered interest {times(cover)} in FY{year}",
                                    {"interest_coverage": times(cover)}, _ids(idx, "interest_coverage", [year])))
        elif (nde := idx.value("net_debt_to_ebitda", year) if year else None) is not None:
            scores["balance_sheet"] = band(-nde, comps["balance_sheet"]["fallback_net_debt_to_ebitda"])
            findings.append(finding("balance_sheet", scores["balance_sheet"],
                                    f"Interest coverage is not reported for FY{year}; net debt was {times(nde)} EBITDA",
                                    {"net_debt_to_ebitda": times(nde)}, _ids(idx, "net_debt_to_ebitda", [year])))

        # Cash generation: share of years with positive free cash flow.
        fcf_years = idx.years("free_cash_flow")[:6]
        if len(fcf_years) >= 3:
            positive = sum(1 for y in fcf_years if idx.value("free_cash_flow", y) > 0)
            share = positive / len(fcf_years)
            scores["cash_generation"] = band(share, comps["cash_generation"]["bands"])
            findings.append(finding("cash_generation", scores["cash_generation"],
                                    f"Free cash flow was positive in {positive} of {len(fcf_years)} years",
                                    {"positive_years": f"{positive} of {len(fcf_years)}"},
                                    _ids(idx, "free_cash_flow", fcf_years)))

    weights = {k: v["weight"] for k, v in comps.items() if not (bank_mode and k in cfg["bank_mode_skip"])}
    result = combine(scores, weights, cfg["missing_limit"])
    return {**result, "components": scores, "findings": findings}


def is_large(valuation_state: dict | None, currency: str | None) -> bool:
    """A large company, for the valuation and forecast bands: market value at or above
    large_company_min_market_value in scoring_bands.yaml (₹1 lakh crore, $50 billion)."""
    v = valuation_state or {}
    mv = (v.get("multiples") or {}).get("market_value") or (v.get("market_value_check") or {}).get("market_value")
    limit = _cfg().get("large_company_min_market_value", {}).get(currency or "")
    return bool(mv and limit and mv >= limit)


def valuation(peers: dict | None, *, bank_mode: bool, large: bool = False) -> dict:
    """From verification's peer comparison: premium to the median, scored and averaged.
    A large company is scored on the wider large_company_bands (being dearer counts less)."""
    cfg = _cfg()["valuation"]
    bands = cfg.get("large_company_bands", cfg["bands"]) if large else cfg["bands"]
    comps = (peers or {}).get("comparisons") or {}
    wanted = cfg["bank_multiples"] if bank_mode else cfg["multiples"]
    scores, findings = {}, []
    names = {"pe": "P/E", "ev_ebitda": "EV/EBITDA", "pb": "P/B", "ps": "P/S"}

    def score_multiples(multiples: list[str], note: str = "") -> None:
        for m in multiples:
            c = comps.get(m)
            if not c or c.get("premium") is None:
                continue
            # Version 6: price-driven scores slide instead of jumping (scoring_common.smooth_band).
            scores[m] = smooth_band(c["premium"], bands) if cfg.get("smooth") else band(c["premium"], bands)
            side = "dearer" if c["premium"] > 0 else "cheaper"
            findings.append(finding(f"valuation_{m}", scores[m],
                                    f"{names[m]} {c['company']:.1f} against a peer median of {c['median']:.1f} "
                                    f"({c['peers_used']} peers), {abs(c['premium']) * 100:.0f}% {side}"
                                    + (" (large-company bands)" if large else "") + note,
                                    {"company": f"{c['company']:.1f}", "median": f"{c['median']:.1f}",
                                     "premium": pct(c["premium"], 0, signed=True)},
                                    [f"computed:peer_median_{m}"]))

    score_multiples(wanted)
    # Fallback (decided 7 October, Eternal): when too few peers are profitable for a P/E or
    # EV/EBITDA median, the price is still compared on P/B and P/S where those have one,
    # so a very dear stock is not left unscored. Marked as a fallback: the analysis step
    # then treats it as low reliability (half weight, confidence one step lower).
    fallback: list[str] = []
    if not scores and not bank_mode:
        score_multiples(cfg.get("fallback_multiples", []),
                        "; used because too few peers have a P/E or EV/EBITDA to compare (most make losses)")
        fallback = list(scores)
    if not scores:
        return {"score": None, "components": {}, "findings": [], "missing": wanted}
    return {"score": round(sum(scores.values()) / len(scores), 4), "components": scores, "findings": findings,
            "missing": [m for m in wanted if m not in scores], "fallback": fallback}
