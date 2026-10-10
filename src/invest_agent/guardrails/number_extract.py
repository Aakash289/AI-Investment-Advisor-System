"""
guardrails/number_extract.py: finds the numbers in a sentence and the facts it cites (design 14.12).

What this file does:
    numbers()   every number in a piece of text that needs a source: percentages, rupee
                and dollar amounts (with crore, lakh, billion, trillion), multiples
                ("21.5x"), points, and plain figures, in Indian (10,85,997) or Western
                (1,085,997) digit grouping. Each comes back with its value, how many
                decimals were shown, its unit and its scale, so two numbers can be
                compared the way a reader would ("12.4%" matches 12.43%).
                Years, dates, fiscal-year labels, durations, 8-K item codes and the fact
                tags themselves are exempt (exempt_patterns.yaml).
    tags()      the fact ids a sentence cites, from tags such as [F3] or [F3, F7].
    sentences() splits a paragraph into sentences, keeping each sentence's tags with it
                even when the model put the tag after the full stop.
    matches()   whether a number in the text is one of the numbers written in a fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from invest_agent.common.yaml_loader import load_yaml

TAG_RE = re.compile(r"\[(F\d+(?:\s*,\s*F\d+)*)\]")
NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<sign>[+\-−])?"
    r"(?P<cur>₹|\$|Rs\.?\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>\s?%|x\b|×|\s(?:crore|lakh|billion|trillion|million|points?|times)\b)?",
    re.IGNORECASE)
SCALES = {"crore": 1e7, "lakh": 1e5, "million": 1e6, "billion": 1e9, "trillion": 1e12}


@dataclass(frozen=True)
class Number:
    text: str
    value: float          # absolute value as written, before scale
    decimals: int
    unit: str             # pct, times, points, INR, USD or plain
    scale: float

    @property
    def amount(self) -> float:
        return self.value * self.scale


def _config() -> dict:
    return load_yaml("guardrails/exempt_patterns.yaml")


def strip_exempt(text: str) -> str:
    for pattern in _config()["remove"]:
        text = re.sub(pattern, " ", text)
    return text


def numbers(text: str) -> list[Number]:
    cfg = _config()
    low, high = cfg["year_range"]
    out = []
    for m in NUMBER_RE.finditer(strip_exempt(text)):
        raw = m.group("num")
        value = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = (m.group("suffix") or "").strip().lower()
        cur = (m.group("cur") or "").strip()
        unit, scale = "plain", 1.0
        if suffix == "%":
            unit = "pct"
        elif suffix in ("x", "×", "times"):
            unit = "times"
        elif suffix in ("point", "points"):
            unit = "points"
        elif suffix in SCALES:
            scale = SCALES[suffix]
        if cur:
            unit = "INR" if cur.startswith(("₹", "Rs")) else "USD"
        if unit == "plain" and scale == 1.0 and decimals == 0 and "," not in raw and low <= value <= high:
            continue                                  # a year
        out.append(Number(m.group(0).strip(), value, decimals, unit, scale))
    return out


def tags(text: str) -> list[str]:
    ids: list[str] = []
    for group in TAG_RE.findall(text):
        ids += [x.strip() for x in group.split(",")]
    return ids


def sentences(paragraph: str) -> list[str]:
    """Split into sentences. A tag written just after the full stop is moved before it."""
    text = re.sub(r"([.!?])\s*(\[F\d+(?:\s*,\s*F\d+)*\](?:\s*\[F\d+(?:\s*,\s*F\d+)*\])*)", r" \2\1", paragraph.strip())
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z₹$\"'(])", text)
    return [p.strip() for p in parts if p.strip()]


def matches(n: Number, candidates: list[Number]) -> bool:
    """True if `n` is one of the candidate numbers, as shown: the candidate rounded to the
    decimals in `n`, within half of the last digit shown, in the same unit (a plain
    number may stand for a multiple, and a sign is not compared; the meaning check
    covers direction)."""
    tolerance = 0.5 * 10 ** (-n.decimals) * n.scale + 1e-9
    for c in candidates:
        same_unit = c.unit == n.unit or {c.unit, n.unit} <= {"plain", "times"} or n.unit == "plain"
        if not same_unit:
            continue
        if abs(abs(c.amount) - abs(n.amount)) <= tolerance:
            return True
    return False
