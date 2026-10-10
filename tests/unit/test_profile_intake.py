"""
Unit tests for profile intake: validation, merging with the stored profile, the one
combined follow-up question, defaults after asking once, the currency default, the
profile store, and the intake node itself (with a fake model and an in-memory
database). No API key or network needed. Run with:
    uv run pytest tests/unit/test_profile_intake.py -v
"""

import asyncio

import pytest

from invest_agent.agents.profile_intake import ProfileExtraction, make_profile_intake_node
from invest_agent.profile.followup import (
    apply_currency_default, apply_defaults, combined_question, merge_profiles, missing_required,
)
from invest_agent.profile.store import ProfileStore
from invest_agent.profile.validate import validate_profile
from invest_agent.storage.db import connect


@pytest.fixture
def store():
    return ProfileStore(connect(":memory:"))


# --- validate, merge, missing, question, defaults ------------------------------------

def test_validation_drops_values_that_make_no_sense():
    clean, problems = validate_profile({"risk_tolerance": "HIGH", "concentration_limit_pct": 150,
                                        "budget_amount": -5, "horizon_years": 10, "goal": None})
    assert clean == {"risk_tolerance": "high", "horizon_years": 10.0}
    assert len(problems) == 2


def test_new_values_replace_stored_and_stored_fill_gaps():
    stored = {"risk_tolerance": "low", "horizon_years": 10, "budget_amount": 100_000, "budget_currency": "INR"}
    merged = merge_profiles(stored, {"budget_amount": 300_000})
    assert merged["budget_amount"] == 300_000 and merged["risk_tolerance"] == "low"
    assert merged["budget_currency"] == "INR" and merged["budget_currency_assumed"] is True


def test_missing_fields_and_one_combined_question():
    missing = missing_required({"risk_tolerance": "low"})
    assert missing == ["budget", "horizon_years", "goal"]
    question = combined_question(missing, "Apple Inc.")
    assert question.startswith("Before I analyse Apple Inc.") and question.count("?") == 0
    assert "one message" in question


def test_defaults_after_asking_once_never_include_the_budget():
    profile, assumed = apply_defaults({"budget_amount": 1000}, ["risk_tolerance", "goal"])
    assert profile["risk_tolerance"] == "medium" and profile["goal"] == "growth"
    assert set(assumed) == {"risk_tolerance", "goal", "liquidity_needs"}


def test_currency_default_from_the_listing():
    out = apply_currency_default({"budget_amount": 50_000}, "USD")
    assert out["budget_currency"] == "USD" and out["budget_currency_assumed"] is True
    assert apply_currency_default({"budget_amount": 1, "budget_currency": "INR"}, "USD")["budget_currency"] == "INR"


def test_store_saves_only_what_the_user_said(store):
    store.save({"risk_tolerance": "low", "assumed_fields": ["goal"], "budget_currency_assumed": True})
    assert store.load() == {"risk_tolerance": "low"}


# --- the node ----------------------------------------------------------------------------

def run_node(answer: ProfileExtraction, store, state=None):
    async def fake_extract(message):
        return answer
    node = make_profile_intake_node(fake_extract, lambda: store)
    return asyncio.run(node(state or {"raw_input": "x"}))


def test_full_profile_in_one_message(store):
    out = run_node(ProfileExtraction(risk_tolerance="low", horizon_years=10, goal="growth",
                                     budget_text="2 lakhs"), store)
    assert out["followup_question"] == ""
    assert out["user_profile"]["budget_amount"] == 200_000 and out["user_profile"]["budget_currency"] == "INR"
    assert store.load()["budget_amount"] == 200_000          # remembered for next time


def test_missing_fields_ask_once_then_use_defaults(store):
    first = run_node(ProfileExtraction(budget_text="$5,000"), store, {"raw_input": "x", "company_name": "Apple Inc."})
    assert first["followup_asked"] is True and "Apple Inc." in first["followup_question"]
    second = run_node(ProfileExtraction(), store, {"raw_input": "just analyse it", "followup_asked": True})
    assert second["followup_question"] == ""
    assert second["user_profile"]["risk_tolerance"] == "medium"
    assert "risk_tolerance" in second["user_profile"]["assumed_fields"]


def test_ambiguous_budget_is_confirmed_first(store):
    out = run_node(ProfileExtraction(risk_tolerance="high", horizon_years=5, goal="growth",
                                     budget_text="10 to 20k"), store)
    assert out["budget_needs_confirmation"] is True and "10 to 20k" in out["followup_question"]
    assert "budget_amount" not in out["user_profile"]


def test_stored_profile_means_no_question(store):
    store.save({"risk_tolerance": "medium", "horizon_years": 7, "goal": "income",
                "budget_amount": 300_000, "budget_currency": "INR"})
    out = run_node(ProfileExtraction(), store, {"raw_input": "should I invest in TCS"})
    assert out["followup_question"] == "" and out["user_profile"]["goal"] == "income"


def test_missing_budget_after_asking_runs_without_an_amount(store):
    out = run_node(ProfileExtraction(risk_tolerance="low", horizon_years=3, goal="income"), store,
                   {"raw_input": "x", "followup_asked": True})
    assert out["missing_profile_fields"] == ["budget"] and out["followup_question"] == ""