"""
Unit tests for profile/budget_parser.py: the budget words a user types, turned into an
exact amount and currency, and the two kinds of phrasing that must be confirmed
(ranges and mixed units, decided 4 October). Run with:
    uv run pytest tests/unit/test_budget_parser.py -v
"""

import pytest

from invest_agent.profile.budget_parser import confirmation_question, format_amount, parse_budget


# pytest.mark.parametrize runs the same test once per row: here, one row per phrase.
@pytest.mark.parametrize("text, amount, currency", [
    ("2 lakhs", 200_000, "INR"),
    ("2 lakh", 200_000, "INR"),
    ("1.5 crore", 15_000_000, "INR"),
    ("Rs 50,000", 50_000, "INR"),
    ("₹2,00,000", 200_000, "INR"),
    ("$5,000", 5_000, "USD"),
    ("5000 dollars", 5_000, "USD"),
    ("USD 1.2 million", 1_200_000, "USD"),
    ("50k", 50_000, None),          # no currency: filled in later, marked as assumed
    ("Rs 50k", 50_000, "INR"),
    ("about 300000", 300_000, None),
])
def test_clear_amounts_go_straight_through(text, amount, currency):
    parsed = parse_budget(text)
    assert not parsed.needs_confirmation
    assert parsed.amount == amount and parsed.currency == currency


@pytest.mark.parametrize("text, reason", [
    ("10 to 20k", "range"),
    ("2-3 lakh", "range"),
    ("$5 lakh", "mixed_units"),
    ("50k and 2 lakh", "several_amounts"),
    ("5L", "short_unit"),
    ("2 cr", "short_unit"),
])
def test_ranges_and_mixed_units_ask(text, reason):
    parsed = parse_budget(text)
    assert parsed.needs_confirmation and parsed.reason == reason
    assert confirmation_question(parsed)          # always a question to ask


def test_short_unit_question_shows_the_amount():
    assert "₹5,00,000" in confirmation_question(parse_budget("5L"))


def test_no_number():
    parsed = parse_budget("a decent amount")
    assert parsed.amount is None and parsed.reason == "no_number" and not parsed.needs_confirmation


def test_indian_and_western_formatting():
    assert format_amount(200_000, "INR") == "₹2,00,000"
    assert format_amount(15_000_000, "INR") == "₹1,50,00,000"
    assert format_amount(500, "INR") == "₹500"
    assert format_amount(200_000, "USD") == "$200,000"