"""
agents/profile_intake.py: the Profile and Budget Intake node.

What this file does:
    Reads the same raw message as Entity Resolution, at the same time (they run in
    parallel), and works out the user's profile: risk tolerance, horizon, goal,
    concentration limit, liquidity needs and budget. Step by step:

      1. One small Luna call returns the fields the message mentions; the budget comes
         back as the user's exact words ("2 lakhs"), never as a number.
      2. budget_parser.py turns those words into an amount and currency, plain code.
      3. validate.py drops any value that makes no sense.
      4. The result is merged with the stored profile and saved (store.py).
      5. Then one of three things happens:
           - the budget is ambiguous (a range, mixed units): ask the user to confirm it;
           - required fields are missing and we have not asked yet: ask once, in one
             combined question;
           - otherwise the profile is ready; anything still missing after asking once
             takes its default and is marked as assumed (the budget never is).

    The model only reads text into fields. Arithmetic, checks, storage and the
    decision about what to ask are all plain code (design 14.2).

How it is wired:
    make_profile_intake_node() returns the node. The model call and the profile store
    can be swapped for fakes, which is how the unit tests run without a key.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from invest_agent.config import get_settings
from invest_agent.graph.state import GraphState
from invest_agent.profile.budget_parser import confirmation_question, parse_budget
from invest_agent.profile.followup import apply_defaults, combined_question, merge_profiles, missing_required
from invest_agent.profile.store import ProfileStore
from invest_agent.profile.target_parser import parse_target
from invest_agent.profile.validate import validate_profile

logger = logging.getLogger(__name__)


class ProfileExtraction(BaseModel):
    """The model's answer: every field may be null when the message does not say it.
    Literal[...] limits a field to the listed words, so the model cannot invent a new level."""

    risk_tolerance: Literal["low", "medium", "high"] | None = Field(default=None)
    horizon_years: float | None = Field(default=None, description="Years invested, as a number.")
    goal: Literal["growth", "income", "capital_preservation"] | None = Field(default=None)
    concentration_limit_pct: float | None = Field(default=None, description="Max % of money in one stock.")
    liquidity_needs: bool | None = Field(default=None)
    budget_text: str | None = Field(default=None, description="The budget words exactly as the user wrote them.")
    # A return target in the question ("10% in the next 2 years"); for this question only,
    # never stored between sessions (analysis/target.py).
    target_return_pct: float | None = Field(default=None, description="Return asked for, in percent.")
    target_return_basis: Literal["per_year", "total"] | None = Field(default=None)
    target_years: float | None = Field(default=None, description="Years the target is for.")


ExtractFn = Callable[[str], Awaitable[ProfileExtraction]]


def _load_prompt() -> str:
    return resources.files("invest_agent.agents").joinpath("prompts/profile_extraction.txt").read_text(encoding="utf-8")


async def extract_with_model(message: str) -> ProfileExtraction:
    """The one Luna call for this node."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env; Profile Intake needs it.")
    model = ChatOpenAI(
        model=settings.model_retrieval,
        api_key=settings.openai_api_key,
        max_retries=settings.max_model_retries,
        timeout=30,
    ).with_structured_output(ProfileExtraction)
    return await model.ainvoke([SystemMessage(content=_load_prompt()), HumanMessage(content=message)])


def make_profile_intake_node(extract: ExtractFn = extract_with_model,
                             store_factory: Callable[[], ProfileStore] = ProfileStore):
    """Build the LangGraph node. Fakes can be passed in for tests."""

    async def profile_intake(state: GraphState) -> dict:
        ref = await extract(state.get("raw_input", ""))

        # Step 2: budget words to an amount and currency, in code.
        new_fields = ref.model_dump(exclude={"budget_text"})   # model_dump: pydantic object to a dict
        # A return target read in code too, filling whatever the model left empty (it
        # skipped the target in two live runs, 6 October).
        for key, value in parse_target(state.get("raw_input", "")).items():
            if new_fields.get(key) is None:
                new_fields[key] = value
        budget = parse_budget(ref.budget_text) if ref.budget_text else None
        if budget and not budget.needs_confirmation and budget.amount is not None:
            new_fields["budget_amount"] = budget.amount
            new_fields["budget_currency"] = budget.currency

        # Step 3: drop anything that makes no sense.
        clean, problems = validate_profile(new_fields)
        if problems:
            logger.info("Profile values dropped: %s", problems)

        # Step 4: merge with what is stored, and save what the user actually said.
        store = store_factory()
        profile = merge_profiles(store.load(), clean)
        # A return target is for this question only (not saved), but it carries over to the
        # reply when a follow-up question was asked in between.
        earlier = state.get("user_profile") or {}
        if "target_return_pct" not in clean and earlier.get("target_return_pct") is not None:
            for key in ("target_return_pct", "target_return_basis", "target_years"):
                if earlier.get(key) is not None:
                    profile[key] = earlier[key]
        store.save(profile)

        # Step 5a: an ambiguous budget is confirmed before anything else.
        if budget and budget.needs_confirmation:
            return {
                "user_profile": profile,
                "budget_needs_confirmation": True,
                "followup_question": confirmation_question(budget),
            }

        # Step 5b: ask once for everything still missing.
        missing = missing_required(profile)
        if missing and not state.get("followup_asked"):
            return {
                "user_profile": profile,
                "budget_needs_confirmation": False,
                "missing_profile_fields": missing,
                "followup_question": combined_question(missing, state.get("company_name")),
                "followup_asked": True,
            }

        # Step 5c: ready. After asking once, defaults fill the gaps (never the budget).
        profile, assumed = apply_defaults(profile, [m for m in missing if m != "budget"])
        if assumed:
            profile["assumed_fields"] = assumed
        return {
            "user_profile": profile,
            "budget_needs_confirmation": False,
            "missing_profile_fields": [m for m in missing if m == "budget"],
            "followup_question": "",
        }

    return profile_intake


profile_intake_node = make_profile_intake_node()
