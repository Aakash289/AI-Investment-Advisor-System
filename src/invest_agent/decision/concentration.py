"""
decision/concentration.py: step 3 of position sizing, the concentration cap (design 14.13).

What this file does:
    No single stock should take more than a set share of the person's total budget. The
    person's own limit applies when they gave one; otherwise the default for their risk
    tolerance does (low 10%, medium 15%, high 25%), and the output says so and invites
    them to set their own. When the cap bites, an overexposure note says what the
    analysis alone would have suggested:

      "This amount is capped at your limit of 10% of your budget (₹20,000). The analysis
       on its own would have suggested ₹30,000."

    Amounts here are in the budget's currency.
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.fx.convert import format_money


def limit_for(profile: dict) -> tuple[float, bool]:
    """(limit as a share of the budget, whether it is the default)."""
    stated = profile.get("concentration_limit_pct")
    if stated:
        return stated / 100, False
    risk = profile.get("risk_tolerance") or "medium"
    return load_yaml("decision/sizing_config.yaml")["risk"][risk]["default_limit"], True


def apply_cap(amount: float, budget: float, currency: str, profile: dict) -> dict:
    """Returns {"amount", "limit", "limit_amount", "default", "capped", "note"}."""
    limit, default = limit_for(profile)
    cap = limit * budget
    capped = amount > cap
    note = None
    if capped:
        whose = "the default limit for your risk tolerance" if default else "your limit"
        note = (f"This amount is capped at {whose} of {limit * 100:g}% of your budget ({format_money(cap, currency)}). "
                f"The analysis on its own would have suggested {format_money(amount, currency)}.")
    return {"amount": min(amount, cap), "limit": limit, "limit_amount": cap, "default": default,
            "capped": capped, "note": note}
