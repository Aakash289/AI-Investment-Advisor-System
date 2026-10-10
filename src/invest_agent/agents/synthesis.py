"""
agents/synthesis.py: the Synthesis Agent (design 14.11).

What this file does:
    1. Builds the numbered facts in the fixed block order (synthesis_facts.py).
    2. Makes the one Terra call of the request: the prompt in prompts/synthesis.txt, the
       facts as the message, the answer in the SynthesisOutput shape. If the answer
       cannot be read, it tries once more, telling the model what went wrong.
    3. Checks the verdict (synthesis_validate.py): one step at most, never past a cap,
       never out of insufficient data, and only with a reason and cited facts.
       Otherwise the scorecard verdict stands and the attempt is logged.
    4. Falls back to the templated reasoning, built from the facts word for word, when
       the call fails twice, when there is no API key, or when the data is insufficient
       (there is nothing for the model to weigh, so no call is made).

    The result goes to the grounding guardrail (guardrails/grounding.py), which checks
    every number before anything is shown.

    Cost: about 8,000 tokens in and 1,500 out, roughly $0.035 a request, most of the
    request's cost (14.16).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from importlib import resources

from langchain_core.messages import HumanMessage, SystemMessage

from invest_agent.agents.synthesis_facts import build_facts, facts_text
from invest_agent.agents.synthesis_validate import (
    SynthesisOutput,
    check_verdict,
    templated,
)
from invest_agent.common.models import structured_call
from invest_agent.config import get_settings
from invest_agent.graph.state import GraphState

logger = logging.getLogger(__name__)

CallFn = Callable[[str, str], Awaitable[SynthesisOutput]]


def prompt() -> str:
    return resources.files("invest_agent.agents").joinpath("prompts/synthesis.txt").read_text(encoding="utf-8")


async def ask_terra(system: str, body: str) -> SynthesisOutput:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    # SYNTHESIS_REASONING_EFFORT (10 October): empty keeps the model's own default, as before;
    # "low" or "medium" can be tried for speed. A refused setting falls back to the default.
    return await structured_call(settings.model_synthesis, SynthesisOutput,
                                 [SystemMessage(content=system), HumanMessage(content=body)],
                                 timeout=120, effort=settings.synthesis_reasoning_effort or "")


def message(state: dict, facts: list[dict]) -> str:
    company = f"{state.get('company_name')} ({state.get('ticker')}, {state.get('exchange')})"
    return (f"Company: {company}\nPreliminary verdict from the scorecard: {state.get('preliminary_verdict')}, "
            f"confidence {state.get('preliminary_confidence')}\n\n{facts_text(facts)}")


def make_synthesis_node(call: CallFn = ask_terra):
    async def synthesis(state: GraphState) -> dict:
        facts = build_facts(state)
        preliminary = state.get("preliminary_verdict") or "insufficient_data"
        confidence = state.get("preliminary_confidence")
        card = state.get("scorecard") or {}
        flags: list[str] = []

        if preliminary == "insufficient_data":
            return {"facts": facts, "synthesis": {**templated(facts, preliminary, confidence),
                                                  "flags": ["no_model_call_insufficient_data"]}}

        system, body = prompt(), message(state, facts)
        out, error = None, None
        for attempt in range(2):
            try:
                text = body if error is None else (body + f"\n\nYour last answer could not be read ({error}). "
                                                   "Answer again in the required shape.")
                out = await call(system, text)
                break
            except Exception as err:
                error = f"{type(err).__name__}: {str(err)[:200]}"
                logger.warning("Synthesis attempt %d failed: %s", attempt + 1, error)
        if out is None:
            return {"facts": facts, "synthesis": {**templated(facts, preliminary, confidence),
                                                  "flags": ["synthesis_failed_templated"], "error": error}}

        decided = check_verdict(out, preliminary, capped=bool(card.get("capped")),
                                fact_ids={f["id"] for f in facts})
        if not decided["accepted"]:
            flags.append(f"verdict_change_rejected: {decided['rejected_because']}")
        syn = {"verdict": decided["verdict"], "confidence": confidence, "changed": decided["changed"],
               "change_reason": decided["reason"], "change_sources": decided["sources"],
               "model_verdict": out.verdict, "reasoning": out.reasoning[:6], "key_risks": out.key_risks[:4],
               "what_would_change_the_view": out.what_would_change_the_view[:3], "templated": False, "flags": flags}
        return {"facts": facts, "synthesis": syn}

    return synthesis


synthesis_node = make_synthesis_node()
