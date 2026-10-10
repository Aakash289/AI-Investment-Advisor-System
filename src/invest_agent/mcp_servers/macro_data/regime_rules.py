"""
mcp_servers/macro_data/regime_rules.py: turns indicators into plain labels (design 14.5).

What this file does:
    A 6.5% policy rate says little on its own; whether it is up from 5.5% or down from
    7.5% says a lot. These rules label each economy's direction, using the thresholds in
    regime_rules.yaml:

      rates        rising, falling or steady: the policy rate's change over 6 months,
                   0.25 percentage points or more either way (one standard central bank move)
      inflation    above, near or below the central bank's target (within 1 point is near)
      growth       accelerating, slowing or steady: for the US the last two quarters'
                   growth against the two before; elsewhere the IMF's estimate for this
                   year against last year
      credit       tightening, easing or steady: the US high-yield spread's change over 3
                   months (US only; India's credit spreads are a known gap)
      yield curve  inverted or not (US only): historically a recession warning

    The labels describe the environment; they are not predictions. F2 in step 9 combines
    them with the company's sector and revenue by country.
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml


def _rules() -> dict:
    return load_yaml("mcp_servers/macro_data/regime_rules.yaml")


def rates(policy: dict | None) -> str | None:
    cfg = _rules()["rates"]
    change = (policy or {}).get(f"change_{cfg['lookback_months']}m")
    if change is None:
        return None
    if change >= cfg["move_threshold_points"]:
        return "rising"
    if change <= -cfg["move_threshold_points"]:
        return "falling"
    return "steady"


def inflation(country: str, infl: dict | None) -> str | None:
    cfg = _rules()
    value = (infl or {}).get("value")
    target = cfg["inflation_targets"].get(country)
    if value is None or target is None:
        return None
    band = cfg["inflation_near_band_points"]
    if value > target + band:
        return "above_target"
    if value < target - band:
        return "below_target"
    return "near_target"


def growth(gdp: dict | None) -> str | None:
    cfg = _rules()["growth"]
    g = gdp or {}
    if "recent_two_quarters" in g and "previous_two_quarters" in g:
        diff, limit = g["recent_two_quarters"] - g["previous_two_quarters"], cfg["quarterly_change_points"]
    elif g.get("value") is not None and g.get("previous") is not None:
        diff, limit = g["value"] - g["previous"], cfg["annual_change_points"]
    else:
        return None
    if diff >= limit:
        return "accelerating"
    if diff <= -limit:
        return "slowing"
    return "steady"


def credit(spread: dict | None) -> str | None:
    cfg = _rules()["credit"]
    change = (spread or {}).get(f"change_{cfg['lookback_months']}m")
    if change is None:
        return None
    if change >= cfg["move_threshold_points"]:
        return "tightening"
    if change <= -cfg["move_threshold_points"]:
        return "easing"
    return "steady"


def yield_curve(curve: dict | None) -> str | None:
    value = (curve or {}).get("value")
    return None if value is None else ("inverted" if value < 0 else "normal")


def label_country(country: str, indicators: dict) -> dict:
    labels = {
        "rates": rates(indicators.get("policy_rate")),
        "inflation": inflation(country, indicators.get("inflation")),
        "growth": growth(indicators.get("gdp_growth")),
        "credit": credit(indicators.get("credit_spread")),
        "yield_curve": yield_curve(indicators.get("yield_curve")),
    }
    return {k: v for k, v in labels.items() if v is not None}
