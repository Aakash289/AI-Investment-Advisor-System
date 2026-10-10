"""
tests/unit/test_backtest.py: the backtesting harness, Route A (design 14.17, build step 15a).

What this file checks:
    scoring        12-month excess return on adjusted prices against the benchmark; the
                   right-or-wrong rules (neutral within +-10%); averages by verdict and
                   whether they come in the right order
    forecasts      base-case error and whether the actual landed in the range; the
                   "range too narrow" flag only with enough cases
    calibration    not enough data, calibrated, and the order wrong
    the run        a decision log with a matured verdict, one still maturing and one
                   question-free insufficient-data entry: only the matured verdict is
                   scored, once; a reported year-1 projection is scored; the report says
                   what is waiting and that tuning needs 100 verdicts

No network: prices and reported figures are fakes, the database is in memory.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

from invest_agent.eval import backtest, calibration, forecast_scoring, scoring
from invest_agent.storage.db import connect


def rows(start: str, days: int, start_price: float, end_price: float) -> list[dict]:
    """Daily rows rising in a straight line from start_price to end_price."""
    d0 = date.fromisoformat(start)
    step = (end_price - start_price) / (days - 1)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "adj_close": start_price + step * i}
            for i in range(days)]


# --- Scoring ----------------------------------------------------------------------------------

def test_excess_return_over_twelve_months():
    stock = rows("2025-01-01", 400, 100, 140)
    bench = rows("2025-01-01", 400, 100, 110)
    r = scoring.excess_return(stock, bench, "2025-01-01", 12)
    assert r["from"] == "2025-01-01" and r["to"] == "2026-01-01"
    assert r["excess"] == pytest.approx(r["stock"] - r["benchmark"]) and r["stock"] > r["benchmark"]
    assert scoring.excess_return([], bench, "2025-01-01", 12) is None


@pytest.mark.parametrize("verdict,excess,right", [
    ("favourable", 0.05, True), ("favourable", -0.01, False),
    ("unfavourable", -0.05, True), ("unfavourable", 0.02, False),
    ("neutral", 0.09, True), ("neutral", -0.12, False),
])
def test_right_or_wrong(verdict, excess, right):
    assert scoring.is_correct(verdict, excess) is right


def test_summary_by_verdict_and_confidence():
    scored = [{"verdict": "favourable", "confidence": "high", "excess": 0.12, "correct": True},
              {"verdict": "favourable", "confidence": "medium", "excess": -0.02, "correct": False},
              {"verdict": "neutral", "confidence": "low", "excess": 0.03, "correct": True},
              {"verdict": "unfavourable", "confidence": "high", "excess": -0.08, "correct": True}]
    s = scoring.summarise(scored)
    assert s["count"] == 4 and s["hit_rate"] == 0.75
    assert s["by_verdict"]["favourable"]["average_excess"] == pytest.approx(0.05)
    assert s["verdicts_in_order"] is True and s["by_confidence"]["high"]["hit_rate"] == 1.0


# --- Forecasts --------------------------------------------------------------------------------

YEAR1 = {"fiscal_year": 2027, "cases": {"low": {"revenue": 90, "net_income": 9}, "base": {"revenue": 100, "net_income": 10},
                                        "high": {"revenue": 110, "net_income": 11}}}


def test_forecast_error_and_range():
    r = forecast_scoring.score_projection(YEAR1, actual_revenue=95, actual_profit=12)
    assert r["revenue"]["error"] == pytest.approx(5 / 95, abs=1e-5) and r["revenue"]["inside"]
    assert r["profit"]["inside"] is False and r["profit"]["error"] == pytest.approx(-2 / 12, abs=1e-5)


def test_range_too_narrow_needs_enough_cases():
    outside = forecast_scoring.score_projection(YEAR1, 150, 15)
    assert forecast_scoring.summarise([outside] * 5)["revenue"]["range_too_narrow"] is None
    assert forecast_scoring.summarise([outside] * 10)["revenue"]["range_too_narrow"] is True


# --- Calibration ------------------------------------------------------------------------------

def test_calibration_states():
    few = {"high": {"count": 5, "hit_rate": 0.8}, "medium": {"count": 30, "hit_rate": 0.6},
           "low": {"count": 30, "hit_rate": 0.4}}
    assert calibration.check(few)["status"] == "not_enough_data"
    good = {k: {**v, "count": 30} for k, v in few.items()}
    assert calibration.check(good)["status"] == "calibrated"
    bad = {**good, "medium": {"count": 30, "hit_rate": 0.9}}
    out = calibration.check(bad)
    assert out["status"] == "not_calibrated" and "Medium verdicts were right more often than high" in out["finding"]


# --- The monthly run --------------------------------------------------------------------------

def _log(conn, request_id, created, verdict, ticker="AAA", year1=None, market="US"):
    payload = {"company": {"market": market, "cik": None}, "year1": year1}
    conn.execute("INSERT INTO decision_log (request_id, created_at, ticker, verdict, confidence, payload_json) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (request_id, created, ticker, verdict, "medium", json.dumps(payload)))
    conn.commit()


def test_monthly_run_scores_matured_verdicts_once():
    conn = connect(":memory:")
    _log(conn, "old", "2025-09-01T10:00:00+00:00", "favourable", year1=YEAR1)
    _log(conn, "new", "2026-09-01T10:00:00+00:00", "neutral")
    _log(conn, "gap", "2025-09-01T10:00:00+00:00", "insufficient_data")

    async def prices(symbol, market):
        return rows("2025-08-01", 450, 100, 130) if symbol == "AAA" else rows("2025-08-01", 450, 100, 105)

    async def actuals(market, ticker, cik):
        return {"revenue": {2027: 104.0}, "net_income": {2027: 9.5}}

    report = asyncio.run(backtest.run(conn, date(2027, 6, 1), prices, actuals))
    v = report["verdicts_this_run"]
    assert [s["ticker"] for s in v["scored_now"]] == ["AAA"] and v["scored_now"][0]["correct"] is True
    assert v["waiting"] == [{"ticker": "AAA", "due": "2027-09-01"}]        # "new" is not 12 months old yet
    assert len(report["forecasts_this_run"]["scored_now"]) == 1
    assert report["summary"]["tuning"].startswith("not yet: 1 of the 100")

    again = asyncio.run(backtest.run(conn, date(2027, 6, 2), prices, actuals))
    assert again["verdicts_this_run"]["scored_now"] == [] and again["summary"]["verdicts"]["count"] == 1
    lines = backtest.text(again)
    assert any(line.startswith("Scored so far: 1 verdict,") for line in lines)


def test_first_year_reports_what_is_waiting():
    conn = connect(":memory:")
    _log(conn, "r1", "2026-10-06T10:00:00+00:00", "neutral", year1={**YEAR1, "fiscal_year": 2027})

    async def never(*args):
        raise AssertionError("nothing should be fetched yet")

    report = asyncio.run(backtest.run(conn, date(2026, 10, 7), never, never))
    assert report["verdicts_this_run"]["waiting"] == [{"ticker": "AAA", "due": "2027-10-06"}]
    assert "first is due on 2027-10-06" in backtest.text(report)[1]
