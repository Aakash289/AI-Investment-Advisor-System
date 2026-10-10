"""
guardrails/grounding.py: the Grounding Guardrail (design 14.12).

What this file does:
    Every number the reader sees must come from the data. Synthesis writes its reasoning
    citing facts as [F3]; this node checks it before anything is shown.

    Check 1, plain code, for every sentence in the reasoning, the key risks, what would
    change the view, and the reason for any verdict change:
      a sentence with a number must cite at least one fact
      every fact it cites must exist
      every number in it must be one of the numbers written in the facts it cites, as
        shown (12.4% matches 12.43%; nothing else is allowed)
      years, dates, durations and 8-K item codes are exempt (exempt_patterns.yaml)

    Check 2, one Luna call (meaning_check.py), on the sentences that passed and contain
    numbers: is each number used with the right meaning and direction? Skipped for the
    templated fallback, which copies the facts word for word.

    What happens to failures:
      the whole sentence is removed, never just the number, and nothing is rewritten
      a paragraph that loses more than half its sentences is dropped
      a key risk or what-would-change item with a failing sentence is dropped
      any removal lowers confidence one step, once per request
      if the reason for a verdict change fails, the change is reversed
      more than three removals replace the reasoning with the templated version
    Every number checked, every result and every removal is logged.
"""

from __future__ import annotations

from invest_agent.agents.synthesis_validate import templated
from invest_agent.analysis.confidence import lower
from invest_agent.graph.state import GraphState
from invest_agent.guardrails import meaning_check
from invest_agent.guardrails.number_extract import matches, numbers, sentences, tags

MAX_REMOVALS = 3


def check_sentence(sentence: str, facts_by_id: dict[str, dict], extra_cites: list[str] | None = None) -> dict:
    """Check 1 for one sentence."""
    cites = list(dict.fromkeys(tags(sentence) + (extra_cites or [])))
    found = numbers(sentence)
    result = {"sentence": sentence, "cites": cites, "numbers": [], "ok": True, "reason": None}
    unknown = [c for c in cites if c not in facts_by_id]
    if unknown:
        return {**result, "ok": False, "reason": f"cites facts that do not exist: {', '.join(unknown)}"}
    if not found:
        return result
    if not cites:
        return {**result, "ok": False, "reason": "a number with no fact cited",
                "numbers": [{"text": n.text, "matched": False} for n in found]}
    pool = [n for c in cites for n in numbers(facts_by_id[c]["text"])]
    checked = [{"text": n.text, "matched": matches(n, pool)} for n in found]
    missing = [c["text"] for c in checked if not c["matched"]]
    if missing:
        return {**result, "numbers": checked, "ok": False,
                "reason": f"not in the cited facts: {', '.join(missing)}"}
    return {**result, "numbers": checked}


def _units(syn: dict) -> list[dict]:
    """Every checkable unit: (part, index, sentences)."""
    units = []
    for part in ("reasoning", "key_risks", "what_would_change_the_view"):
        for i, text in enumerate(syn.get(part) or []):
            units.append({"part": part, "index": i, "sentences": sentences(text)})
    return units


async def ground(syn: dict, facts: list[dict], *, preliminary: str, preliminary_confidence: str | None,
                 ask: meaning_check.MeaningFn = meaning_check.ask_model) -> dict:
    """Check the Synthesis result and apply the failure rules. Returns the state update."""
    facts_by_id = {f["id"]: f for f in facts}
    log: list[dict] = []
    units = _units(syn)
    for u in units:
        u["checks"] = [check_sentence(s, facts_by_id) for s in u["sentences"]]

    reason_check = None
    if syn.get("changed") and syn.get("change_reason"):
        reason_check = check_sentence(syn["change_reason"], facts_by_id, syn.get("change_sources"))

    # Check 2 on the survivors that hold numbers (model-written text only).
    flags = list(syn.get("flags") or [])
    if not syn.get("templated"):
        items, where = [], {}
        for ui, u in enumerate(units):
            for si, c in enumerate(u["checks"]):
                if c["ok"] and c["numbers"]:
                    sid = f"s{len(items) + 1}"
                    items.append({"id": sid, "sentence": c["sentence"], "cites": c["cites"]})
                    where[sid] = (ui, si)
        if reason_check and reason_check["ok"] and reason_check["numbers"]:
            items.append({"id": "reason", "sentence": reason_check["sentence"], "cites": reason_check["cites"]})
        meaning = await meaning_check.check(items, facts_by_id, ask)
        flags += meaning["flags"]
        for sid, res in meaning["results"].items():
            entry = {"check": "meaning", "id": sid, **res}
            log.append(entry)
            if res["result"] != "not_supported":
                continue
            if sid == "reason":
                reason_check = {**reason_check, "ok": False, "reason": f"meaning: {res['reason']}"}
            else:
                ui, si = where[sid]
                units[ui]["checks"][si] = {**units[ui]["checks"][si], "ok": False, "reason": f"meaning: {res['reason']}"}

    # Apply the failure rules.
    removed: list[str] = []
    out: dict = {"reasoning": [], "key_risks": [], "what_would_change_the_view": []}
    for u in units:
        checks = u["checks"]
        log += [{"check": "numbers", "part": u["part"], "index": u["index"], **c} for c in checks]
        failed = [c for c in checks if not c["ok"]]
        removed += [c["sentence"] for c in failed]
        kept = [c for c in checks if c["ok"]]
        if u["part"] == "reasoning":
            if checks and len(failed) > len(checks) / 2:
                continue                                    # paragraph dropped
            if kept:
                out["reasoning"].append(_entry(" ".join(c["sentence"] for c in kept), kept, facts_by_id))
        elif not failed and kept:
            entry = _entry(" ".join(c["sentence"] for c in kept), kept, facts_by_id)
            out[u["part"]].append(entry if u["part"] == "key_risks" else entry["text"])

    verdict, change_reason = syn.get("verdict", preliminary), syn.get("change_reason")
    if reason_check is not None:
        log.append({"check": "change_reason", **reason_check})
        if not reason_check["ok"]:
            removed.append(reason_check["sentence"])
            verdict, change_reason = preliminary, None
            flags.append("verdict_change_reversed")

    confidence = syn.get("confidence") or preliminary_confidence
    if removed and confidence and lower(confidence) != confidence:
        confidence = lower(confidence)
        flags.append("confidence_lowered_by_guardrail")
    if len(removed) > MAX_REMOVALS and not syn.get("templated"):
        fallback = templated(facts, verdict, confidence)
        out["reasoning"] = [_entry(p, [check_sentence(s, facts_by_id) for s in sentences(p)], facts_by_id)
                            for p in fallback["reasoning"]]
        flags.append("reasoning_replaced_by_template")

    return {"verdict": verdict, "confidence": confidence, "verdict_change_reason": change_reason or "",
            "reasoning": out["reasoning"], "key_risks": out["key_risks"],
            "what_would_change_the_view": out["what_would_change_the_view"],
            "guardrail_log": log, "removed_sentences": removed, "guardrail_flags": flags}


def _entry(text: str, checks: list[dict], facts_by_id: dict[str, dict]) -> dict:
    cited = list(dict.fromkeys(c for ch in checks for c in ch["cites"]))
    record_ids = list(dict.fromkeys(r for c in cited for r in facts_by_id[c]["record_ids"]))
    return {"text": text, "facts": cited, "record_ids": record_ids}


def make_grounding_node(ask: meaning_check.MeaningFn = meaning_check.ask_model):
    async def grounding(state: GraphState) -> dict:
        syn = state.get("synthesis") or {}
        if not syn:
            return {}
        return await ground(syn, state.get("facts") or [], preliminary=state.get("preliminary_verdict") or "neutral",
                            preliminary_confidence=state.get("preliminary_confidence"), ask=ask)

    return grounding


grounding_node = make_grounding_node()
