"""
profile/budget_parser.py: turns the user's budget words into an exact amount and currency.

What this file does:
    The model copies the budget phrase out of the message exactly as written
    ("2 lakhs", "Rs 1.5 crore", "$5,000", "50k"). This file, plain code, turns that
    phrase into a number and a currency. A model is never trusted to do the
    arithmetic, because a wrong budget changes the recommended amount directly and
    would not look wrong to anyone downstream (design 14.2).

    Multipliers: lakh or lac = 1,00,000; crore = 1,00,00,000; k or thousand = 1,000;
    m, mn or million = 1,000,000; bn or billion = 1,000,000,000.
    Currency: Rs, INR, rupees or the rupee sign mean INR; $, USD or dollars mean USD;
    lakh and crore written out in full also mean INR. No currency at all is fine: the
    amount is kept and the currency is filled in later from the stored profile or the
    stock's listing, and the output says it was assumed.

    Only two kinds of phrasing stop and ask the user (decided 4 October):
      ranges        "10 to 20k", "2-3 lakh"
      mixed units   "$5 lakh", "50k" next to "lakh", or the short forms "L" and "Cr"
    Everything else goes straight through and is restated at the top of the output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INDIAN_UNITS = {"lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7}
SHORT_INDIAN_UNITS = {"l": 1e5, "cr": 1e7}             # short forms: always confirmed
WESTERN_UNITS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
                 "bn": 1e9, "billion": 1e9}
ALL_UNITS = {**INDIAN_UNITS, **SHORT_INDIAN_UNITS, **WESTERN_UNITS}

# Longest unit names first, so "lakhs" is matched before "l" and "million" before "m".
_UNIT_PATTERN = "|".join(sorted(map(re.escape, ALL_UNITS), key=len, reverse=True))

# A number such as 5000, 5,000, 2,00,000 or 1.5, optionally followed by a unit.
# (?![a-z]) stops "m" matching the start of "months".
_AMOUNT = re.compile(rf"(\d+(?:[.,]\d+)*)\s*({_UNIT_PATTERN})?(?![a-z])")

# Two numbers joined by "-", "to" or "and" as a span: "10 to 20k", "2-3 lakh".
_RANGE = re.compile(r"\d[\d,.]*\s*[a-z]*\s*(?:-|to|or)\s*\d")

_INR_WORDS = re.compile(r"₹|\brs\.?|\binr\b|\brupees?\b")
_USD_WORDS = re.compile(r"\$|\busd\b|\bdollars?\b")


@dataclass
class BudgetParse:
    """The result of reading one budget phrase."""

    original: str
    amount: float | None = None
    currency: str | None = None          # "INR", "USD", or None when not stated
    needs_confirmation: bool = False
    reason: str | None = None            # "range", "mixed_units", "short_unit", "several_amounts", "no_number"


def _to_number(text: str) -> float:
    # Commas are only digit grouping here (Indian "2,00,000" or Western "200,000").
    return float(text.replace(",", ""))


def parse_budget(text: str | None) -> BudgetParse:
    """Read one budget phrase. See the module docstring for the rules."""
    original = (text or "").strip()
    t = original.lower()
    result = BudgetParse(original=original)
    if not t:
        result.reason = "no_number"
        return result

    has_inr = bool(_INR_WORDS.search(t))
    has_usd = bool(_USD_WORDS.search(t))

    if _RANGE.search(t):
        result.needs_confirmation, result.reason = True, "range"
        return result

    matches = _AMOUNT.findall(t)
    if not matches:
        result.reason = "no_number"
        return result
    if len(matches) > 1:
        result.needs_confirmation, result.reason = True, "several_amounts"
        return result

    number_text, unit = matches[0]
    unit = unit or ""
    amount = _to_number(number_text) * ALL_UNITS.get(unit, 1)

    indian = unit in INDIAN_UNITS or unit in SHORT_INDIAN_UNITS
    western = unit in WESTERN_UNITS
    if (has_usd and indian) or (has_inr and has_usd) or (indian and western):
        result.needs_confirmation, result.reason = True, "mixed_units"
        return result
    if unit in SHORT_INDIAN_UNITS:
        result.needs_confirmation, result.reason = True, "short_unit"
        result.amount, result.currency = amount, "INR"   # kept, so the question can show it
        return result

    result.amount = amount
    if has_inr or unit in INDIAN_UNITS:
        result.currency = "INR"
    elif has_usd:
        result.currency = "USD"
    return result


def format_amount(amount: float, currency: str | None) -> str:
    """Show an amount the way the user would write it: Indian grouping for rupees
    (₹2,00,000), Western grouping for dollars ($200,000)."""
    if currency == "INR":
        whole = f"{amount:.0f}"
        last3, rest = whole[-3:], whole[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        return "₹" + ",".join(groups + [last3]) if groups else "₹" + last3
    symbol = "$" if currency == "USD" else ""
    return f"{symbol}{amount:,.0f}"


def confirmation_question(parse: BudgetParse) -> str:
    """The one question asked when a budget phrase is ambiguous."""
    said = f'"{parse.original}"'
    if parse.reason == "range":
        return f"You mentioned {said} as your budget. What single amount, with its currency, should I use as the total you have to invest?"
    if parse.reason == "short_unit" and parse.amount is not None:
        return f"Just to check, by {said} do you mean {format_amount(parse.amount, 'INR')} in total to invest?"
    if parse.reason == "several_amounts":
        return f"You mentioned more than one amount ({said}). What is the total you have to invest, with its currency?"
    return f"{said} mixes currencies or units. What is the total you have to invest, as one amount with its currency (for example ₹5,00,000 or $5,000)?"