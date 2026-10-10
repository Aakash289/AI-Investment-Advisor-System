"""
Unit tests for build step 6, the conversion side: fx/convert.py, fx/risk_note.py and
the FX node in fx/step.py.

They use the real Frankfurter answers saved by scripts/fx_probe.py in tests/fixtures/fx/,
and fake fx_server calls, so no network is needed. Run with:
    uv run pytest tests/unit/test_fx_convert.py -v
"""

import asyncio
import json
from pathlib import Path

import pytest

from invest_agent.fx.convert import convert, convert_figures, describe_rate, format_money
from invest_agent.fx.risk_note import risk_note
from invest_agent.fx.step import make_fx_node
from invest_agent.mcp_servers.fx.frankfurter import parse_rate, parse_series

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fx"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def usd_inr():
    """Today's USD to INR rate in the shape fx_server returns, from the real answer."""
    found = parse_rate(load("frankfurter_usd_inr_latest.json"), "USD", "INR")
    return {"base": "USD", "quote": "INR", **found, "source": "frankfurter", "provider": "official blend",
            "tier": 1, "flags": []}


# --- Parsing the real Frankfurter answers ----------------------------------------------

def test_real_answers_parse():
    assert usd_inr()["rate"] == 96.2 and usd_inr()["date"] == "2026-10-05"
    assert parse_rate(load("frankfurter_usd_inr_fbil.json"), "USD", "INR")["date"] == "2026-09-28"
    assert parse_rate({"date": "2026-10-02", "rates": {"INR": 96.0}}, "USD", "INR")["rate"] == 96.0   # older shape
    points = parse_series(load("frankfurter_usd_inr_12m.json"))
    assert points[0] == {"date": "2025-10-01", "rate": 88.34} and len(points) >= 11


# --- Conversion ------------------------------------------------------------------------

def test_budget_converts_both_ways_and_keeps_the_original():
    rate = usd_inr()
    to_usd = convert(200_000, "INR", "USD", rate)
    assert to_usd["converted"]["amount"] == pytest.approx(200_000 / 96.2)
    assert to_usd["original"] == {"amount": 200_000, "currency": "INR"}
    assert to_usd["rate_date"] == "2026-10-05" and to_usd["indicative"] is True
    back = convert(to_usd["converted"]["amount"], "USD", "INR", rate)
    assert back["converted"]["amount"] == pytest.approx(200_000)       # full precision kept


def test_same_currency_is_a_no_op_and_wrong_pair_refuses():
    same = convert(500.0, "INR", "INR", usd_inr())
    assert same["converted"]["amount"] == 500.0 and same["indicative"] is False
    with pytest.raises(ValueError):
        convert(100, "EUR", "INR", usd_inr())


def test_adr_style_figures_convert_and_missing_ones_are_skipped():
    out = convert_figures({"eps": 10.0, "book_value_per_share": None}, "INR", "USD", usd_inr())
    assert set(out) == {"eps"} and out["eps"]["converted"]["amount"] == pytest.approx(10 / 96.2)


def test_rounding_happens_only_for_display():
    assert format_money(200_000, "INR") == "₹2,00,000"
    assert format_money(200_000 / 96.2, "USD") == "$2,079.00"
    assert format_money(12.5, "JPY") == "12.50 JPY"
    assert describe_rate(usd_inr()) == "1 USD = 96.20 INR (Frankfurter, official blend, 2026-10-05)"


# --- Currency risk note -------------------------------------------------------------------

def test_risk_note_from_the_real_series():
    note = risk_note(parse_series(load("frankfurter_usd_inr_12m.json")), usd_inr(), "INR", "USD")
    assert note.startswith("Over the past 12 months the dollar rose 8.9% against the rupee (₹88.34 to ₹96.20 per dollar).")
    assert note.endswith("This is context, not a forecast.")


def test_risk_note_fall_flat_and_missing():
    rate = usd_inr()
    assert "fell 3.8%" in risk_note([{"date": "2025-10-01", "rate": 100.0}], rate, "INR", "USD")
    assert "barely moved" in risk_note([{"date": "2025-10-01", "rate": 96.0}], rate, "INR", "USD")
    assert risk_note([], rate, "INR", "USD") is None


# --- The FX node -------------------------------------------------------------------------

def run_node(state, rate=None, series=None, fail=False):
    calls = []

    async def get_rate(base, quote):
        calls.append(("rate", base, quote))
        if fail:
            raise RuntimeError("server down")
        return rate or usd_inr()

    async def get_series(base, quote, months):
        calls.append(("series", base, quote, months))
        return series or {"points": parse_series(load("frankfurter_usd_inr_12m.json")), "flags": []}

    return asyncio.run(make_fx_node(get_rate, get_series)(state)), calls


APPLE_FOR_RUPEES = {"listing_currency": "USD", "user_profile": {"budget_amount": 200_000, "budget_currency": "INR"}}


def test_node_same_currency_makes_no_calls():
    out, calls = run_node({"listing_currency": "INR", "user_profile": {"budget_amount": 1, "budget_currency": "INR"}})
    assert out == {"fx": {"needed": False}} and calls == []


def test_node_converts_the_budget_and_records_the_rate():
    out, calls = run_node(APPLE_FOR_RUPEES)
    assert calls[0] == ("rate", "USD", "INR")                    # rupees per dollar
    fx = out["fx"]
    assert fx["budget_converted"]["converted"]["amount"] == pytest.approx(2079.0, abs=0.01)
    assert "dollar rose 8.9%" in fx["risk_note"]
    record = out["fx_records"][0]
    assert record["record_id"] == "frankfurter:USDINR:fx_rate" and record["unit"] == "INR per USD"
    assert record["as_of"] == "2026-10-05"


def test_node_without_a_rate_carries_on_in_the_stock_currency():
    out, _ = run_node(APPLE_FOR_RUPEES, fail=True)
    fx = out["fx"]
    assert "budget_converted" not in fx and "fx_unavailable" in fx["flags"]
    assert any("amounts are given in USD only" in n for n in fx["notes"])
    assert "fx_records" not in out
