"""
profile/followup.py: merges the new message with the stored profile and works out what is still missing.

What this file does:
    1. merge_profiles(): the stored profile fills the gaps, and anything the user says
       in this message replaces what was stored ("my budget is now 3 lakhs").
    2. missing_required(): which of the required fields (budget, risk tolerance,
       horizon, goal) are still missing after the merge.
    3. combined_question(): the one follow-up question listing everything missing,
       asked once (decided 4 October). It is a template, not a model call.
    4. apply_defaults(): if something is still missing after asking once, the defaults
       in intake_config.yaml fill it in and are listed as assumed; the budget is never
       assumed, the analysis simply runs without an amount.
    5. apply_currency_default(): a budget with no currency takes the stored profile's
       currency, otherwise the stock's listing currency. It runs after Entity
       Resolution, which supplies that currency, so it is called by the graph (step 4).

Design reference: 14.2.
"""

from __future__ import annotations

from typing import Any

from invest_agent.common.yaml_loader import load_yaml

# What each field is called in the follow-up question, in the order asked.
QUESTION_PARTS = {
    "budget": "how much you have to invest in total, with the currency (for example ₹2,00,000 or $5,000)",
    "risk_tolerance": "how much risk you are comfortable with: low, medium or high",
    "horizon_years": "how many years you plan to stay invested",
    "goal": "your main goal: growth, regular income, or protecting what you have",
}

STORED_FIELDS = ("risk_tolerance", "horizon_years", "goal", "concentration_limit_pct",
                 "liquidity_needs", "budget_amount", "budget_currency")


def _config() -> dict:
    return load_yaml("profile/intake_config.yaml")


def merge_profiles(stored: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """New values replace stored ones; stored values fill whatever the message did not say.
    A new budget amount without a currency keeps the stored currency."""
    merged = {k: v for k, v in stored.items() if k in STORED_FIELDS and v is not None}
    for key, value in new.items():
        if value is not None:
            merged[key] = value
    if new.get("budget_amount") is not None:
        # A new amount with no currency of its own takes the stored currency (the first
        # step of the currency default rule), and is marked as assumed.
        merged["budget_currency_assumed"] = new.get("budget_currency") is None and "budget_currency" in merged
    return merged


def missing_required(profile: dict[str, Any]) -> list[str]:
    """Required fields still missing, in the order they are asked about."""
    missing = []
    for field in _config()["required_fields"]:
        if field == "budget":
            if not profile.get("budget_amount"):
                missing.append("budget")
        elif profile.get(field) is None:
            missing.append(field)
    return missing


def combined_question(missing: list[str], company_name: str | None = None) -> str:
    """One question for everything missing, so the user answers once."""
    parts = [QUESTION_PARTS[m] for m in missing if m in QUESTION_PARTS]
    if not parts:
        return ""
    about = f" {company_name}" if company_name else " this"
    if len(parts) == 1:
        listed = parts[0]
    else:
        listed = "; ".join(parts[:-1]) + "; and " + parts[-1]
    return (f"Before I analyse{about}, I need a little about you: {listed}. "
            "You can answer everything in one message.")


def apply_defaults(profile: dict[str, Any], missing: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Fill fields still missing after asking once. Returns (profile, assumed field names)."""
    defaults = _config()["defaults_after_asking_once"]
    filled = dict(profile)
    assumed = []
    for field in missing:
        if field in defaults:
            filled[field] = defaults[field]
            assumed.append(field)
    if filled.get("liquidity_needs") is None:
        filled["liquidity_needs"] = defaults.get("liquidity_needs", False)
        assumed.append("liquidity_needs")
    return filled, assumed


def apply_currency_default(profile: dict[str, Any], listing_currency: str | None) -> dict[str, Any]:
    """Give a currency-less budget the listing's currency, marked as assumed."""
    if profile.get("budget_amount") and not profile.get("budget_currency") and listing_currency:
        profile = dict(profile)
        profile["budget_currency"] = listing_currency
        profile["budget_currency_assumed"] = True
    return profile