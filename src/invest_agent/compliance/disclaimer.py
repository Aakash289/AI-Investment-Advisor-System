"""
compliance/disclaimer.py: the Compliance and Disclaimer step, and the answer the user reads
(design 14.14, 14.15).

What this file does:
    The last node before the answer is shown. It:
      1. removes any sentence with a banned phrase from the written reasoning
         (phrase_check.py)
      2. works out the situation lines that apply: amount withheld, insufficient data, no
         budget, partial analysis, not covered, and "assumes you do not already own this
         stock" whenever an amount is given
      3. writes the compact recommendation: the company and profile, the verdict and
         confidence, the amount and shares, why (the checked reasoning), the key risks,
         what would change the view, the fair value range labelled as a projection, the
         currency note for a cross-border case, and the data quality line when it
         changed the outcome
      4. wraps it: the short line first, the full disclaimer last, and records the
         disclaimer version shown

    Hard rule: no disclaimer means no output. If the disclaimer text is missing or did
    not make it into the answer, the answer is replaced by a short refusal, and nothing
    else is shown.

    The fact tags ([F3]) are taken out of the text the user reads; they stay in the
    state and the decision log, and the PDF report (step 13) turns them into numbered
    source references.

    Plain code, no model call.
"""

from __future__ import annotations

import logging
import re

from invest_agent.analysis.scorecard import near_line, reliability_reason
from invest_agent.analysis.target import target_lines
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.compliance import phrase_check
from invest_agent.decision.sizing import HOLDING_NOTE
from invest_agent.fx.convert import format_money
from invest_agent.graph.state import GraphState
from invest_agent.profile.budget_parser import format_amount

logger = logging.getLogger(__name__)

TAG = re.compile(r"\s*\[F\d+(?:\s*,\s*F\d+)*\]")
VERDICT_WORDS = {"favourable": "Favourable", "neutral": "Neutral", "unfavourable": "Unfavourable",
                 "insufficient_data": "Insufficient data", "not_covered": "Not covered"}
ASSUMED = {"liquidity_needs": "you will not need this money at short notice",
           "risk_tolerance": "a medium risk tolerance", "horizon_years": "a 5-year horizon",
           "goal": "your goal is growth"}
REFUSAL = "The answer could not be shown because its disclaimer is missing. Nothing else has been shown."


class DisclaimerMissing(Exception):
    pass


def config() -> dict:
    return load_yaml("compliance/disclaimer.yaml")


def untag(text: str) -> str:
    """Take the fact tags out of text the user reads."""
    return re.sub(r"\s+([.,;:])", r"\1", TAG.sub("", text)).strip()


def situation_lines(state: dict) -> list[str]:
    s = config()["situations"]
    lines = []
    sizing = state.get("sizing") or {}
    if state.get("verdict") == "not_covered":
        return [s["not_covered"].format(reason=state.get("not_covered_reason") or "it is outside version 1")]
    if state.get("verdict") == "insufficient_data" or state.get("preliminary_verdict") == "insufficient_data":
        lines.append(s["insufficient_data"])
    if sizing.get("status") == "withheld":
        reason = sizing.get("reason") or ""
        if reason == "no budget was provided":
            lines.append(s["no_budget"])
        elif "not complete enough for a verdict" not in reason:
            lines.append(s["amount_withheld"].format(reason=reason))
    if state.get("partial_analysis"):
        lines.append(s["partial_analysis"])
    if sizing.get("status") == "ok":
        lines.append(s["existing_holding"])
    return lines


def wrap(body: str, situations: list[str]) -> str:
    """Short line first, situation lines, full disclaimer last. Raises if either is missing."""
    cfg = config()
    short, full = (cfg.get("short") or "").strip(), (cfg.get("full") or "").strip()
    if not short or not full:
        raise DisclaimerMissing("disclaimer text is missing from disclaimer.yaml")
    parts = [short, "", body.strip()]
    if situations:
        parts += ["", *situations]
    parts += ["", "Disclaimer: " + full]
    text = "\n".join(parts)
    if not (text.startswith(short) and text.rstrip().endswith(full)):
        raise DisclaimerMissing("the disclaimer did not make it into the answer")
    return text


def _amount_lines(state: dict) -> list[str]:
    s = state.get("sizing") or {}
    status = s.get("status")
    if status == "withheld":
        return []                                      # the situation line says why
    if status == "zero":
        return ["Suggested amount: none", *[f"  {n}" for n in s.get("notes", [])]]
    if status != "ok":
        return []
    cur, stock = s["currency"], s["stock_currency"]
    if s["shares"] == 0:
        conv = f" (about {format_money(s['suggested_stock'], stock)})" if stock != cur else ""
        line = (f"Suggested amount: about {format_money(s['suggested'], cur)}{conv}, less than the price of one "
                f"share ({format_money(s['price'], stock)})")
    else:
        conv = f" (about {format_money(s['amount_stock'], stock)}, indicative)" if stock != cur else ""
        line = (f"Suggested amount: about {format_money(s['amount'], cur)}{conv}, which buys {s['shares']} "
                f"shares at {format_money(s['price'], stock)}")
    notes = [n for n in s.get("notes", []) if n != HOLDING_NOTE]
    notes += [s[k] for k in ("overexposure_note", "liquidity_note") if s.get(k)]
    return [line, *[f"  {n}" for n in notes]]


def _fair_value_line(state: dict) -> str | None:
    fv = state.get("fair_value") or {}
    if fv.get("weak_peers"):
        return f"No fair value is given, because {fv.get('reason')}."
    if fv.get("status") != "ok":
        return None
    cur = state.get("listing_currency") or "USD"
    r = fv["range"]
    line = (f"Estimated fair value range (a projection, not a reported figure): {format_money(r.get('low', r['base']), cur)} "
            f"to {format_money(r.get('high', r['base']), cur)} per share, based on next year's projected earnings at "
            f"the multiples similar companies trade at today. Actual results will differ.")
    why = reliability_reason(state.get("scorecard"), "forecast")
    if why:
        line += f" This range is low reliability ({why})."
    return line


def compact(state: dict, cleaned: dict) -> str:
    """The answer the user reads, before the disclaimer is wrapped round it."""
    profile = state.get("user_profile") or {}
    lines = []
    company = f"{state.get('company_name')} ({state.get('ticker')}, {state.get('exchange')})"
    budget = format_amount(profile["budget_amount"], profile.get("budget_currency")) \
        if profile.get("budget_amount") else "no budget given"
    horizon = profile.get("horizon_years")
    horizon = f"{horizon:g} years" if isinstance(horizon, (int, float)) else "horizon not given"
    goal = str(profile.get("goal") or "goal not given").replace("_", " ")
    lines.append(f"{company}, for {budget}, {profile.get('risk_tolerance') or 'medium'} risk tolerance, "
                 f"{horizon}, goal {goal}")
    if profile.get("assumed_fields"):
        lines.append("Assumed: " + "; ".join(ASSUMED.get(a, a.replace("_", " ")) for a in profile["assumed_fields"])
                     + " (tell me if that is wrong)")
    lines.append("")

    verdict = state.get("verdict") or state.get("preliminary_verdict")
    if verdict == "insufficient_data":
        card = state.get("scorecard") or {}
        lines.append("Verdict: none, because " + "; ".join(card.get("reasons", ["the data was incomplete"])))
        lines.append("")
        lines.append("What did come back (incomplete):")
        lines += [f"  {untag(p['text'])}" for p in cleaned["reasoning"]]
        return "\n".join(lines)

    lines.append(f"Verdict: {VERDICT_WORDS.get(verdict, verdict)}, confidence {state.get('confidence')}")
    if state.get("verdict_change_reason"):
        lines.append(f"  Moved one step from the scorecard: {untag(state['verdict_change_reason'])}")
    elif near_line(state.get("scorecard")):
        lines.append(f"  {near_line(state.get('scorecard'))}")
    lines += _amount_lines(state)
    fx = state.get("fx") or {}
    if fx.get("needed") and fx.get("risk_note"):
        lines.append(f"Currency: {fx['risk_note']}")
    data_line = (state.get("completeness_detail") or {}).get("user_line")
    if data_line:
        lines.append(f"Data: {data_line}")
    # A return target in the question, set against the fair value range (analysis/target.py).
    target = target_lines(state)
    if target:
        lines += ["", "Your return target:"] + [f"  {t}" for t in target]

    if cleaned["reasoning"]:
        lines += ["", "Why:"]
        for i, p in enumerate(cleaned["reasoning"]):
            lines += ([""] if i else []) + [untag(p["text"])]
    if cleaned["key_risks"]:
        lines += ["", "Key risks:"] + [f"- {untag(r['text'])}" for r in cleaned["key_risks"]]
    if cleaned["what_would_change_the_view"]:
        lines += ["", "What would change the view:"] + [f"- {untag(w)}" for w in cleaned["what_would_change_the_view"]]
    fv = _fair_value_line(state)
    if fv:
        lines += ["", fv]
    return "\n".join(lines)


def compliance_node(state: GraphState) -> dict:
    cfg = config()
    try:
        if not (cfg.get("short") or "").strip() or not (cfg.get("full") or "").strip():
            raise DisclaimerMissing("disclaimer text is missing from disclaimer.yaml")
        if state.get("verdict") == "not_covered":
            # The not-covered situation line, with the company's name, is the whole answer.
            line = situation_lines(state)[0].replace("This company", state.get("company_name") or "This company", 1)
            return {"final_response": wrap(line, []), "disclaimer_version": cfg.get("version"),
                    "situation_lines": [line]}
        cleaned = phrase_check.clean(state)
        situations = situation_lines(state)
        text = wrap(compact(state, cleaned), situations)
    except DisclaimerMissing as err:
        logger.error("No disclaimer, so no output: %s", err)
        return {"final_response": REFUSAL, "disclaimer_version": None, "situation_lines": []}
    return {"final_response": text, "disclaimer_version": cfg.get("version"), "situation_lines": situations,
            "reasoning": cleaned["reasoning"], "key_risks": cleaned["key_risks"],
            "what_would_change_the_view": cleaned["what_would_change_the_view"],
            "phrase_removals": cleaned["removed"]}
