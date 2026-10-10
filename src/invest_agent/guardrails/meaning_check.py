"""
guardrails/meaning_check.py: Check 2, is each number used with the right meaning (design 14.12)?

What this file does:
    Check 1 (grounding.py) proves every number exists in a cited fact. It cannot see a
    real number used with the wrong meaning: "revenue grew 11.2%" when 11.2% is the
    operating margin, or "rose" when it fell. This is one batched Luna call over only the
    sentences that contain numbers, each shown with the facts it cites, answering per
    sentence: supported, not_supported or unclear.

      not_supported   treated as a failure: the sentence is removed
      unclear         logged, the sentence is kept
      the call fails  Check 1 stands on its own, and the result is flagged

    It costs a fraction of a cent. Text built from the facts word for word (the
    templated fallback) skips it, since it cannot misuse a number.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from invest_agent.common.models import structured_call
from invest_agent.config import get_settings


class Verdict(BaseModel):
    id: str
    result: Literal["supported", "not_supported", "unclear"]
    reason: str


class Verdicts(BaseModel):
    items: list[Verdict]


MeaningFn = Callable[[str, str], Awaitable[Verdicts]]


def prompt() -> str:
    return resources.files("invest_agent.guardrails").joinpath("prompts/meaning_check.txt").read_text(encoding="utf-8")


def format_items(items: list[dict], facts_by_id: dict[str, dict]) -> str:
    """items: [{"id", "sentence", "cites": [fact ids]}]."""
    blocks = []
    for it in items:
        cited = "\n".join(f"  [{f}] {facts_by_id[f]['text']}" for f in it["cites"] if f in facts_by_id)
        blocks.append(f"id: {it['id']}\nsentence: {it['sentence']}\ncited facts:\n{cited}")
    return "\n\n".join(blocks)


async def ask_model(system: str, body: str) -> Verdicts:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    # Low reasoning effort (speed, 7 October): checking a number against its fact is a
    # narrow task; common/models.py falls back to the default if the setting is refused.
    return await structured_call(settings.model_guardrail, Verdicts,
                                 [SystemMessage(content=system), HumanMessage(content=body)])


async def check(items: list[dict], facts_by_id: dict[str, dict], ask: MeaningFn = ask_model) -> dict:
    """Returns {"results": {id: {result, reason}}, "flags": [...]}; ids the model invents are ignored."""
    if not items:
        return {"results": {}, "flags": []}
    try:
        verdicts = await ask(prompt(), format_items(items, facts_by_id))
    except Exception as err:            # Check 1 still stands
        return {"results": {}, "flags": [f"meaning_check_failed: {type(err).__name__}"]}
    wanted = {it["id"] for it in items}
    return {"results": {v.id: {"result": v.result, "reason": v.reason} for v in verdicts.items if v.id in wanted},
            "flags": []}
