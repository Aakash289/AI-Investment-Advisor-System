"""
tests/unit/test_number_extract.py: finding numbers and tags in the reasoning (design 14.12).

What this file checks:
    numbers     percentages, rupee and dollar amounts with crore and billion, Indian and
                Western digit grouping, multiples, signs; years, dates, fiscal-year
                labels, durations and 8-K item codes are exempt
    tags        [F3] and [F3, F7]
    sentences   a tag written after the full stop stays with its sentence; decimals do
                not split a sentence
    matches     rounding as shown (12.4% matches 12.43%), units must agree, a plain
                number may stand for a multiple
"""

from __future__ import annotations

from invest_agent.guardrails.number_extract import Number, matches, numbers, sentences, tags


def texts(s):
    return [n.text for n in numbers(s)]


def test_amounts_and_grouping():
    found = numbers("Revenue was ₹10,85,997 crore, against $416.2 billion and 1,085,997 units.")
    assert [(n.unit, n.amount) for n in found] == [("INR", 1085997 * 1e7), ("USD", 416.2e9), ("plain", 1085997.0)]


def test_percent_multiple_points_and_sign():
    found = numbers("Margin up +3.0 points to 31.1%, P/E 21.5x, score -0.31.")
    assert [(n.text, n.unit) for n in found] == [("+3.0 points", "points"), ("31.1%", "pct"), ("21.5x", "times"),
                                                 ("-0.31", "plain")]


def test_exempt_numbers():
    s = "In FY2026, over 10 years and a 5-year span, the 8-K items 1.03 and 4.02 filed on 2026-07-30 in 2025."
    assert texts(s) == []


def test_tags():
    assert tags("Growth was 8.4% [F5, F9] and more [F12].") == ["F5", "F9", "F12"]


def test_sentences_keep_their_tags():
    parts = sentences("Revenue grew 15.5% a year. [F9] The P/E is 21.5 [F11]. Margins held.")
    assert parts == ["Revenue grew 15.5% a year [F9].", "The P/E is 21.5 [F11].", "Margins held."]


def test_matches_as_shown():
    fact = numbers("ROIC 12.43% and P/E 21.5")
    assert matches(numbers("12.4%")[0], fact)
    assert not matches(numbers("12.5%")[0], fact)
    assert matches(Number("21.5x", 21.5, 1, "times", 1.0), fact)      # plain stands for a multiple
    assert not matches(numbers("$21.5")[0], fact)                     # but a currency must agree
