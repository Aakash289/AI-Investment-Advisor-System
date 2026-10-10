"""
profile/validate.py: checks each profile field makes sense before it is used or saved.

What this file does:
    The model's extraction is checked by plain code, never trusted blindly (design
    14.2). Each field has a simple rule; a value that breaks its rule is dropped and
    reported, so it counts as missing and the user is asked for it, instead of a bad
    value flowing into the sizing maths.

      risk_tolerance           one of low, medium, high
      horizon_years            more than 0 and at most 50
      target_return_pct        more than 0 and at most 500 (a return target, in percent)
      target_return_basis      per_year or total
      target_years             more than 0 and at most 50
      goal                     one of growth, income, capital_preservation
      concentration_limit_pct  more than 0 and at most 100 (a percentage of the budget)
      liquidity_needs          true or false
      budget_amount            more than 0
      budget_currency          INR or USD
"""

from __future__ import annotations

from typing import Any

RISK_LEVELS = {"low", "medium", "high"}
GOALS = {"growth", "income", "capital_preservation"}
CURRENCIES = {"INR", "USD"}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_profile(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (the fields that passed, a plain-language note for each one dropped).

    Fields that are None (not mentioned) are simply left out; they are not problems.
    Keys this function does not know about (such as budget_currency_assumed) are kept.
    """
    clean: dict[str, Any] = {}
    problems: list[str] = []

    def keep(name: str, ok: bool, value: Any, note: str) -> None:
        if ok:
            clean[name] = value
        else:
            problems.append(note)

    for name, value in fields.items():
        if value is None:
            continue
        if name == "risk_tolerance":
            v = str(value).lower()
            keep(name, v in RISK_LEVELS, v, f"risk tolerance '{value}' is not low, medium or high")
        elif name == "goal":
            v = str(value).lower()
            keep(name, v in GOALS, v, f"goal '{value}' is not growth, income or capital preservation")
        elif name == "horizon_years":
            v = _number(value)
            keep(name, v is not None and 0 < v <= 50, v, f"horizon of {value} years is outside 0 to 50")
        elif name == "concentration_limit_pct":
            v = _number(value)
            keep(name, v is not None and 0 < v <= 100, v, f"concentration limit of {value}% is outside 0 to 100")
        elif name == "budget_amount":
            v = _number(value)
            keep(name, v is not None and v > 0, v, f"budget of {value} is not a positive amount")
        elif name == "budget_currency":
            v = str(value).upper()
            keep(name, v in CURRENCIES, v, f"currency '{value}' is not INR or USD")
        elif name == "target_return_pct":
            v = _number(value)
            keep(name, v is not None and 0 < v <= 500, v, f"return target of {value}% is outside 0 to 500")
        elif name == "target_return_basis":
            keep(name, value in ("per_year", "total"), value, f"return target basis '{value}' is not per year or total")
        elif name == "target_years":
            v = _number(value)
            keep(name, v is not None and 0 < v <= 50, v, f"target period of {value} years is outside 0 to 50")
        elif name == "liquidity_needs":
            keep(name, isinstance(value, bool), value, f"liquidity needs '{value}' is not yes or no")
        else:
            clean[name] = value
    return clean, problems
