"""
observability/cost_tracker.py: what each request cost in model tokens and dollars (design 14.16).

What this file does:
    Every model call in the app (entity extraction, profile intake, news labelling,
    Synthesis, the meaning check) goes through LangChain, and OpenAI returns the tokens
    used with every answer. LangChain's usage callback adds those up for everything that
    runs inside one block:

        with track_model_usage() as usage:
            result = await graph.ainvoke(state)
        summary = usage_summary(usage.usage_metadata)

    usage_summary() prices the totals from observability/pricing.yaml (OpenAI has no
    pricing API, so prices are typed in with the date they were checked) and returns:

        {"by_model": {"gpt-5.6-terra": {"input_tokens": ..., "output_tokens": ...,
                                         "cost_usd": 0.0312}, ...},
         "total_usd": 0.0351, "unpriced_models": []}

    A model with no price in pricing.yaml is listed in unpriced_models and counted as
    zero, so a renamed model shows up rather than silently costing nothing. There is no
    dollar cap (decided 4 October); runaway protection is the 50-step graph limit and
    the 2 retries per model call.

The library:
    langchain_core.callbacks.get_usage_metadata_callback is a context manager. Every
    LangChain model call made inside it, including inside LangGraph nodes and parallel
    tasks, reports its usage_metadata (input and output tokens) to the handler it yields.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.callbacks import (
    UsageMetadataCallbackHandler,
    get_usage_metadata_callback,
)

from invest_agent.common.yaml_loader import load_yaml


def _prices() -> dict[str, dict]:
    return load_yaml("observability/pricing.yaml").get("models", {}) or {}


def price_for(model_name: str) -> dict | None:
    """Prices for a model. OpenAI reports names with a date suffix
    (gpt-5.6-terra-2026-09-30), so the longest price-table name the reported name starts
    with is used."""
    prices = _prices()
    matches = [k for k in prices if model_name == k or model_name.startswith(k + "-")]
    return prices[max(matches, key=len)] if matches else None


def usage_summary(usage: dict[str, Any]) -> dict:
    """Price the per-model token totals from the usage callback."""
    by_model, total, unpriced = {}, 0.0, []
    for model, u in (usage or {}).items():
        tin, tout = int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
        price = price_for(model)
        if price is None:
            unpriced.append(model)
            cost = 0.0
        else:
            # Prices are per million tokens. Reasoning tokens are already inside
            # output_tokens and are billed as output.
            cost = tin / 1e6 * float(price["input"]) + tout / 1e6 * float(price["output"])
        by_model[model] = {"input_tokens": tin, "output_tokens": tout, "cost_usd": round(cost, 6)}
        total += cost
    return {"by_model": by_model, "total_usd": round(total, 6), "unpriced_models": unpriced}


@contextmanager
def track_model_usage() -> Iterator[UsageMetadataCallbackHandler]:
    """Collect token usage for every model call made inside the block."""
    with get_usage_metadata_callback() as handler:
        yield handler


def cost_line(summary: dict) -> str:
    """One short line for the terminal, e.g. "model cost $0.0351 (2 models)"."""
    n = len(summary.get("by_model") or {})
    if not n:
        return "no model calls"
    extra = f", unpriced: {', '.join(summary['unpriced_models'])}" if summary.get("unpriced_models") else ""
    return f"model cost ${summary['total_usd']:.4f} ({n} model{'s' if n != 1 else ''}{extra})"
