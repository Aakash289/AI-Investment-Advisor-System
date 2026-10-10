"""
compliance/phrase_check.py: removes sentences with banned phrases (design 14.15).

What this file does:
    Promises such as "guaranteed", "risk-free", "can't lose" or "buy now" have no place
    in an analysis that is not advice. Any sentence in the written reasoning, the key
    risks or what would change the view that contains one (as whole words, any case) is
    removed whole, the same way the grounding guardrail removes failing sentences, and
    the removal is logged.

    Text inside quotation marks is not checked: news headlines are shown quoted with
    their outlet, and the agent did not write them.

    The list lives in disclaimer.yaml, so it can grow without touching code.
"""

from __future__ import annotations

import re

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.guardrails.number_extract import sentences

QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|'[^']{12,}'")


def _pattern() -> re.Pattern:
    phrases = load_yaml("compliance/disclaimer.yaml")["banned_phrases"]

    def one(phrase: str) -> str:
        # Words may be split by spaces or hyphens; an apostrophe may be straight or curly.
        words = [re.escape(w).replace("'", "['\u2019]") for w in re.split(r"[\s-]+", phrase)]
        return r"[\s-]+".join(words)

    alternatives = "|".join(one(p) for p in phrases)
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)


def banned_in(sentence: str) -> str | None:
    """The banned phrase a sentence contains outside quotation marks, or None."""
    m = _pattern().search(QUOTED.sub(" ", sentence))
    return m.group(0) if m else None


def clean_text(text: str) -> tuple[str, list[dict]]:
    """Remove failing sentences from one paragraph. Returns (text, removals)."""
    kept, removed = [], []
    for s in sentences(text):
        phrase = banned_in(s)
        if phrase:
            removed.append({"sentence": s, "phrase": phrase})
        else:
            kept.append(s)
    return " ".join(kept), removed


def clean(state: dict) -> dict:
    """Check reasoning, key risks and what would change the view.
    Returns {"reasoning", "key_risks", "what_would_change_the_view", "removed"}."""
    removed: list[dict] = []
    out: dict = {"reasoning": [], "key_risks": [], "what_would_change_the_view": []}
    for part in out:
        for item in state.get(part) or []:
            text = item["text"] if isinstance(item, dict) else item
            cleaned, gone = clean_text(text)
            removed += [{**g, "part": part} for g in gone]
            if cleaned:
                out[part].append({**item, "text": cleaned} if isinstance(item, dict) else cleaned)
    out["removed"] = removed
    return out
