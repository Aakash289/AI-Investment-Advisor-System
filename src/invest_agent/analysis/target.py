"""
analysis/target.py: the person's return target, set against what the analysis found.

What this file does:
    A question can carry a return target: "I want 10% returns in the next 2 years". The
    intake step reads it into three profile fields (decided 6 October, after the Star
    Health run, where the target was silently dropped):

      target_return_pct     the return asked for, as a percentage (10)
      target_return_basis   "total" (10% over the whole period) or "per_year"
      target_years          the period, in years; the horizon when not said

    describe() turns that into one plain line, with the yearly and total equivalents:
      "10% over 2 years (about 4.9% a year)"

    target_lines() sets it beside the analysis, as facts and not advice:
      the base, low and high fair value a year out against today's price (a projection),
      the stock's yearly swing, and the 5-year past growth when there is no fair value.
    It never says whether to buy, and it does not compare other investments.

    Plain code, no model call.
"""

from __future__ import annotations

from invest_agent.verification.record_index import RecordIndex


def describe(profile: dict | None) -> dict | None:
    """{"pct", "basis", "years", "annual", "total", "text"}, or None when no target was given."""
    p = profile or {}
    pct = p.get("target_return_pct")
    if pct is None:
        return None
    years = p.get("target_years") or p.get("horizon_years") or 1
    rate = pct / 100
    if p.get("target_return_basis") == "per_year":
        annual, total = rate, (1 + rate) ** years - 1
        text = f"{pct:g}% a year for {years:g} year{'s' if years != 1 else ''} (about {total * 100:.1f}% in total)"
    else:
        total = rate
        annual = (1 + rate) ** (1 / years) - 1 if years > 0 and rate > -1 else rate
        text = (f"{pct:g}% over {years:g} year{'s' if years != 1 else ''}"
                + (f" (about {annual * 100:.1f}% a year)" if years != 1 else ""))
    return {"pct": pct, "basis": p.get("target_return_basis") or "total", "years": years,
            "annual": annual, "total": total, "text": text}


def target_lines(state: dict) -> list[str]:
    """The target against the fair value range and the stock's swings; empty without a target."""
    t = describe(state.get("user_profile"))
    if t is None:
        return []
    lines = [f"You asked for {t['text']}."]
    fv = state.get("fair_value") or {}
    up = fv.get("upside") or {}
    if fv.get("status") == "ok" and "base" in up:
        base = up["base"]
        verdict = ("above" if base > t["annual"] + 0.005 else "below" if base < t["annual"] - 0.005
                   else "about the same as")
        line = (f"The base case fair value a year out (a projection) is {base * 100:+.0f}% from today's price, "
                f"{verdict} the {t['annual'] * 100:.1f}% a year your target needs")
        if "low" in up and "high" in up:
            line += f"; the low case is {up['low'] * 100:+.0f}% and the high case {up['high'] * 100:+.0f}%"
        lines.append(line + ".")
    else:
        idx = RecordIndex(state.get("verified_records") or [])
        past = idx.value("price_cagr_5y")
        why = fv.get("reason")
        lines.append("There is no fair value" + (f", because {why}" if why else "") + ". So the analysis cannot "
                     "set an estimate against your target"
                     + (f". Over the last 5 years the share price grew {past * 100:+.1f}% a year, which does not "
                        f"predict the next {t['years']:g} year{'s' if t['years'] != 1 else ''}" if past is not None else "") + ".")
    vol = RecordIndex(state.get("verified_records") or []).value("volatility_1y")
    if vol is not None:
        lines.append(f"The share price swings about {vol * 100:.0f}% a year, so over {t['years']:g} "
                     f"year{'s' if t['years'] != 1 else ''} it can end well above or below any estimate. A fixed "
                     f"return over a short period is not something a single share can promise; that is what "
                     f"products such as bank deposits are built for, and this analysis does not compare them.")
    return lines
