"""
profile/target_parser.py: reads a return target out of the message in plain code.

What this file does:
    The model is asked for the return target (target_return_pct, target_return_basis,
    target_years), but in two live Star Health runs on 6 October it left them empty while
    reading the same message correctly in a one-off check. A figure that changes the answer
    should not depend on that, so this parser reads the message too, the same way the
    budget is read in code (budget_parser.py). The model's values win when it gives them;
    this only fills the gaps.

    What it recognises:
      a percentage next to a return word        "10% returns", "returns of 10%",
                                                "a 12 percent gain", "grow 15%"
      a yearly rate                             "12% a year", "per year", "annually",
                                                "p.a.", "per annum", "yearly"
      the period                                "in the next 2 years", "over 3 years",
                                                "within 18 months"

    What it ignores: a percentage with no return word near it ("no more than 10% in one
    stock" is a concentration limit), and targets without a figure ("good returns").
"""

from __future__ import annotations

import re

_PCT = r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s*cent\b)"
_RETURN_WORDS = r"(?:returns?|gains?|profits?|growth|grow|growing|earn|earning|yield)"
# A return word within a few words either side of the percentage.
_NEAR = [re.compile(_PCT + r"(?:\W+\w+){0,3}?\W+" + _RETURN_WORDS, re.IGNORECASE),
         re.compile(_RETURN_WORDS + r"(?:\W+\w+){0,4}?\W+" + _PCT, re.IGNORECASE)]
_PER_YEAR = re.compile(r"\b(?:a|per|each|every)\s+(?:year|annum)\b|\bannually\b|\byearly\b|\bp\.?\s?a\.?(?=\W|$)",
                       re.IGNORECASE)
_PERIOD = re.compile(r"\b(?:in|over|within|for)\s+(?:the\s+)?(?:next\s+)?(\d+(?:\.\d+)?)\s*(years?|yrs?|months?)\b",
                     re.IGNORECASE)


def parse_target(text: str | None) -> dict:
    """{"target_return_pct", "target_return_basis", "target_years"} for whatever the
    message states; an empty dict when it states no return figure."""
    if not text:
        return {}
    match = next((m for m in (p.search(text) for p in _NEAR) if m), None)
    if not match:
        return {}
    out: dict = {"target_return_pct": float(match.group(1))}
    # Look for the basis and the period in the rest of that sentence.
    sentence = re.split(r"[.?!]", text[match.start():], maxsplit=1)[0] + " " + \
        re.split(r"[.?!]", text[:match.start()])[-1]
    out["target_return_basis"] = "per_year" if _PER_YEAR.search(sentence) else "total"
    period = _PERIOD.search(sentence)
    if period:
        n = float(period.group(1))
        out["target_years"] = n / 12 if period.group(2).lower().startswith("month") else n
    return out
