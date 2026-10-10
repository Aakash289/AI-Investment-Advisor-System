"""
agents/synthesis_validate.py: the plain-code checks on the Synthesis answer (design 14.11).

What this file does:
    SynthesisOutput  the shape the model must answer in (structured output, so the
                     answer arrives as checked fields, not free text to parse)
    check_verdict    the model may keep the scorecard verdict or move it by exactly one
                     step, and only with a reason and the facts behind it. Rejected, with
                     the scorecard verdict kept and the attempt logged, when it:
                       moves two steps (favourable to unfavourable)
                       changes a verdict that an override or suitability rule capped
                       changes "insufficient data", or moves to it
                       gives no reason, or cites no facts, or cites facts that do not exist
    templated        the fallback reasoning built from the facts word for word, used when
                     the model fails twice, when there is no key, when the data is
                     insufficient (no model call is needed), or when the grounding
                     guardrail removes more than three sentences. Flagged as templated.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ORDER = ["unfavourable", "neutral", "favourable"]


class SynthesisOutput(BaseModel):
    verdict: Literal["favourable", "neutral", "unfavourable", "insufficient_data"]
    changed_from_scorecard: bool
    change_reason: str | None = None
    change_sources: list[str] = Field(default_factory=list)
    reasoning: list[str] = Field(description="Paragraphs; every sentence with a number ends with its fact tags")
    key_risks: list[str] = Field(default_factory=list)
    what_would_change_the_view: list[str] = Field(default_factory=list)


def check_verdict(out: SynthesisOutput, preliminary: str, *, capped: bool, fact_ids: set[str]) -> dict:
    """Returns {"verdict", "accepted": bool, "changed": bool, "reason", "sources", "rejected_because"}."""
    keep = {"verdict": preliminary, "accepted": False, "changed": False, "reason": None, "sources": []}
    if out.verdict == preliminary:
        return {**keep, "accepted": True}
    if preliminary == "insufficient_data" or out.verdict == "insufficient_data":
        return {**keep, "rejected_because": "insufficient data cannot be changed by the model"}
    if capped:
        return {**keep, "rejected_because": "the verdict was capped by an override or suitability rule"}
    if abs(ORDER.index(out.verdict) - ORDER.index(preliminary)) > 1:
        return {**keep, "rejected_because": "a change of more than one step"}
    sources = [s for s in out.change_sources if s in fact_ids]
    if not out.change_reason or not out.change_reason.strip():
        return {**keep, "rejected_because": "a change without a reason"}
    if not sources:
        return {**keep, "rejected_because": "a change without a cited fact"}
    return {"verdict": out.verdict, "accepted": True, "changed": True, "reason": out.change_reason.strip(),
            "sources": sources}


def _clean(text: str) -> str:
    """A fact's text as a sentence: the trailing score note kept, a full stop added."""
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text


def templated(facts: list[dict], verdict: str, confidence: str | None) -> dict:
    """Reasoning, risks and what-would-change built from the facts word for word."""
    def para(block: str, lead: str) -> str | None:
        rows = [f for f in facts if f["block"] == block]
        if not rows:
            return None
        return lead + " " + " ".join(f"{_clean(f['text'])} [{f['id']}]." for f in rows)

    scorecard = [f for f in facts if f["block"] == "scorecard"]
    paragraphs = []
    if scorecard:
        paragraphs.append(" ".join(f"{_clean(f['text'])} [{f['id']}]." for f in scorecard))
    for block, lead in (("business", "The business:"), ("valuation", "Valuation:"),
                        ("forecast", "The forecast, which is an estimate:"), ("macro", "The economy:"),
                        ("news", "Recent news, which counts for less than the rest:")):
        p = para(block, lead)
        if p:
            paragraphs.append(p)
    risks = [f"{_clean(f['text'])} [{f['id']}]." for f in facts
             if f["kind"] == "note" and f["block"] in ("valuation", "forecast")]
    risks += [f"{_clean(f['text'])} [{f['id']}]." for f in facts
              if f["block"] in ("business", "valuation", "macro") and "score -2" in f["text"]][:3]
    return {"verdict": verdict, "confidence": confidence, "reasoning": paragraphs, "key_risks": risks[:4],
            "what_would_change_the_view": [], "templated": True}
