"""
analysis/macro_score.py: F2, how well the economic weather suits this company (design 14.18).

What this file does:
    Combines three things gathered earlier:
      the regime labels for each economy (Macro agent: rates rising, inflation above
      target, growth slowing...), the company's sector sensitivity (sector_sensitivity.yaml)
      and the share of its revenue from each economy (Geographic and Sector agent).

    1. For each economy with at least 10% of revenue, each factor scores +1 when it is
       favourable for the company, -1 when unfavourable, 0 when neutral, times the
       sector's sensitivity to it (High 1, Medium 0.5, Low 0):
         rates        falling is favourable (rising, for sectors that benefit from higher
                      rates, such as banks); steady is neutral
         inflation    near or below target is favourable, above is unfavourable
         growth       accelerating favourable, slowing unfavourable
         credit       easing favourable, tightening unfavourable (growth sensitivity)
         yield curve  inverted counts -1 (growth sensitivity); a normal curve is neutral
       An economy's total is capped at +-2.
    2. Each economy's score is weighted by its share of revenue; the rest of the world
       counts as 0, since there is no single economy to read.
    3. For a company earning more than 25% of revenue abroad, a home currency that
       weakened by 2% or more over 12 months adds +1 x currency sensitivity (foreign
       earnings are worth more at home); one that strengthened subtracts.

    The result is capped at +-2 and comes with a finding per economy.
"""

from __future__ import annotations

from invest_agent.analysis.scoring_common import finding, pct
from invest_agent.common.yaml_loader import load_yaml


def sensitivity(sector: str | None, industry: str | None, market: str | None) -> dict | None:
    table = load_yaml("analysis/sector_sensitivity.yaml")
    row = (table["sectors"].get(sector or "") or None)
    if row is None:
        return None
    row = dict(row)
    for o in table.get("industry_overrides", []):
        if o["match"].lower() in (industry or "").lower() and o.get("market", market) == market:
            row.update({k: v for k, v in o.items() if k not in ("match", "market")})
    return row


def country_score(regimes: dict, sens: dict, values: dict) -> tuple[float, list[str]]:
    """One economy: (score capped at +-2, the factors that counted)."""
    score, used = 0.0, []

    def add(points: int, weight_name: str, label: str):
        nonlocal score
        w = values[sens[weight_name]]
        if points and w:
            score += points * w
            used.append(f"{label} ({'+' if points > 0 else '-'}{w:g})")

    rates = regimes.get("rates")
    if rates in ("rising", "falling"):
        good = (rates == "rising") if sens.get("benefits_from_rising") else (rates == "falling")
        add(1 if good else -1, "rates", f"rates {rates}")
    infl = regimes.get("inflation")
    if infl:
        add(-1 if infl == "above_target" else 1, "inflation", f"inflation {infl.replace('_', ' ')}")
    growth = regimes.get("growth")
    if growth in ("accelerating", "slowing"):
        add(1 if growth == "accelerating" else -1, "growth", f"growth {growth}")
    credit = regimes.get("credit")
    if credit in ("easing", "tightening"):
        add(1 if credit == "easing" else -1, "growth", f"credit {credit}")
    if regimes.get("yield_curve") == "inverted":
        add(-1, "growth", "yield curve inverted")
    return max(-2.0, min(2.0, score)), used


def macro_fit(panel: dict | None, geo: dict | None, *, sector: str | None, industry: str | None,
              market: str | None) -> dict:
    cfg = load_yaml("analysis/scoring_bands.yaml")["macro_fit"]
    countries = (panel or {}).get("countries") or {}
    weights = (geo or {}).get("weights") or {}
    sens = sensitivity(sector, industry, market)
    if not countries or not weights or sens is None:
        missing = [n for n, ok in (("macro panel", countries), ("revenue by economy", weights), ("sector", sens)) if not ok]
        return {"score": None, "findings": [], "missing": missing}

    values = cfg["sensitivity_values"]
    total, findings = 0.0, []
    for code, share in sorted(weights.items(), key=lambda kv: -kv[1]):
        if code == "ROW" or share < cfg["min_country_share"] or code not in countries:
            continue
        s, used = country_score(countries[code].get("regimes") or {}, sens, values)
        total += share * s
        name = countries[code].get("name", code)
        findings.append(finding(f"macro_{code}", round(s, 2),
                                f"{name} ({pct(share)} of revenue): {', '.join(used) or 'no factor moves the score'}",
                                {"share": pct(share), "country_score": f"{s:+.2f}"},
                                [f"computed:{code}:regime_{k}" for k in (countries[code].get("regimes") or {})]))

    # The currency rule for companies earning a lot abroad.
    home = "US" if market == "US" else "IN"
    abroad = 1 - weights.get(home, 0)
    currency = ((countries.get(home) or {}).get("indicators") or {}).get("currency") or {}
    move = currency.get("change_12m_pct")
    if abroad > cfg["export_share_for_currency"] and move is not None and values[sens["currency"]]:
        # India: rupees per dollar up = rupee weaker. US: the dollar index up = dollar stronger.
        weaker = move / 100 >= cfg["currency_move_threshold"] if home == "IN" else move / 100 <= -cfg["currency_move_threshold"]
        stronger = move / 100 <= -cfg["currency_move_threshold"] if home == "IN" else move / 100 >= cfg["currency_move_threshold"]
        if weaker or stronger:
            adj = values[sens["currency"]] * (1 if weaker else -1)
            total += adj
            findings.append(finding("macro_currency", adj,
                                    f"{pct(abroad)} of revenue comes from abroad and the home currency "
                                    f"{'weakened' if weaker else 'strengthened'} over 12 months "
                                    f"({move:+.1f}% on {currency.get('series', 'the index')})",
                                    {"abroad": pct(abroad), "move": f"{move:+.1f}%"}, [f"fred:{home}:currency"]))
    return {"score": round(max(-2.0, min(2.0, total)), 4), "findings": findings, "missing": [],
            "sensitivity": sens}
