"""
common/models.py: one way to make the quick structured Luna calls (speed, 7 October 2026).

What this file does:
    News labelling and the meaning check are sorting tasks (label each item, check each
    sentence), yet with the model's default thinking they took about 25 and 8 to 30
    seconds. structured_call() asks for the answer in the given shape with a lower
    reasoning effort (FAST_REASONING_EFFORT in .env, "low" by default).

    If the model or the library refuses the setting, the call is made again without it,
    and that is remembered for the rest of the process, so a refusal costs one extra
    round trip once rather than breaking labelling.
"""

from __future__ import annotations

import logging
from typing import Any

# langchain_openai.ChatOpenAI: LangChain's wrapper around OpenAI's chat models;
# with_structured_output makes the model answer in a pydantic shape.
from langchain_openai import ChatOpenAI

from invest_agent.config import get_settings

logger = logging.getLogger(__name__)

_effort_refused: set[str] = set()          # models that refused reasoning_effort in this process


def _model(name: str, schema: Any, effort: str | None, timeout: int):
    settings = get_settings()
    kwargs: dict[str, Any] = {"model": name, "api_key": settings.openai_api_key,
                              "max_retries": settings.max_model_retries, "timeout": timeout}
    if effort:
        kwargs["reasoning_effort"] = effort
    return ChatOpenAI(**kwargs).with_structured_output(schema)


def _is_effort_refusal(err: Exception) -> bool:
    text = str(err).lower()
    return "reasoning_effort" in text or "reasoning effort" in text or "reasoning.effort" in text


async def structured_call(name: str, schema: Any, messages: list, *, timeout: int = 60,
                          effort: str | None = None) -> Any:
    """One model call answering in `schema`, with the fast reasoning effort when allowed."""
    effort = get_settings().fast_reasoning_effort if effort is None else effort
    if effort and name not in _effort_refused:
        try:
            return await _model(name, schema, effort, timeout).ainvoke(messages)
        except Exception as err:            # noqa: BLE001 - only a refusal of the setting is retried
            if not _is_effort_refusal(err):
                raise
            logger.info("%s does not take reasoning_effort=%s; calling again without it", name, effort)
            _effort_refused.add(name)
    return await _model(name, schema, None, timeout).ainvoke(messages)
