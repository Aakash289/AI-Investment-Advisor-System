"""
tests/unit/test_step14.py: cost, quotas, source health, timings and the decision log (design 14.16, 14.19).

What this file checks:
    cost tracker     prices from pricing.yaml, dated model names, an unpriced model listed
    timings          each node's duration recorded, async and plain nodes alike
    quotas           80% warning, 100% "out", remaining units
    health monitor   down after 3 failures in the window, skipped by tracked_call without
                     calling, back after the cooldown, a success resets the count
    decision log     one row with the year-1 projections; questions are not logged; the
                     clean-up runs at most once a month
    run_turn         a whole request through main.py with a fake graph: cost, timings
                     and the decision log entry all written

No network and no model: every outside piece is a fake or an in-memory database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from invest_agent.observability import decision_log, health_monitor, quotas
from invest_agent.observability import tracked_call as tc
from invest_agent.observability.cost_tracker import cost_line, price_for, usage_summary
from invest_agent.observability.timings import slowest, start_timings, timed
from invest_agent.storage.db import add_quota_units, connect


@pytest.fixture
def db():
    return connect(":memory:")


# --- Cost tracker -----------------------------------------------------------------------------

def test_cost_from_token_counts():
    usage = {"gpt-5.6-terra-2026-09-30": {"input_tokens": 8000, "output_tokens": 1500},
             "gpt-5.6-luna": {"input_tokens": 3000, "output_tokens": 500}}
    s = usage_summary(usage)
    # Terra $2 in, $12 out per million; Luna $0.20 and $1.20.
    assert s["by_model"]["gpt-5.6-terra-2026-09-30"]["cost_usd"] == pytest.approx(0.016 + 0.018)
    assert s["total_usd"] == pytest.approx(0.034 + 0.0006 + 0.0006)
    assert cost_line(s) == "model cost $0.0352 (2 models)"


def test_unknown_model_is_listed_not_hidden():
    s = usage_summary({"gpt-9-mystery": {"input_tokens": 100, "output_tokens": 100}})
    assert s["unpriced_models"] == ["gpt-9-mystery"] and "unpriced: gpt-9-mystery" in cost_line(s)
    assert price_for("gpt-5.6-lunar") is None          # a prefix alone is not a match


# --- Timings ----------------------------------------------------------------------------------

def test_timed_nodes_record_their_duration():
    async def slow(state):
        await asyncio.sleep(0.05)
        return {"a": 1}

    def quick(state):
        return {"b": 2}

    async def run():
        log = start_timings()
        assert await timed("slow", slow)({}) == {"a": 1}
        assert timed("quick", quick)({}) == {"b": 2}
        return log
    log = asyncio.run(run())
    assert [t["node"] for t in log] == ["slow", "quick"] and slowest(log, 1)[0][0] == "slow"


# --- Quotas -----------------------------------------------------------------------------------

def test_quota_warnings_and_remaining(db):
    add_quota_units(db, "trendlyne", 1650)      # of 2,000 a month and 400 a day
    add_quota_units(db, "tavily", 120)
    lines = quotas.warnings(db)
    assert any(line.startswith("Trendlyne: 1,650 of 2,000 calls used this month (82%)") for line in lines)
    assert any("Trendlyne: 1,650 of 400 calls used today" in line and "limit is reached" in line for line in lines)
    assert not any(line.startswith("Tavily") for line in lines)
    assert quotas.remaining("tavily", "month", db) == 880


# --- Health monitor ---------------------------------------------------------------------------

def test_source_marked_down_then_back():
    t = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    assert not health_monitor.record_failure("yfinance", t)
    assert not health_monitor.record_failure("yfinance", t + timedelta(minutes=2))
    assert health_monitor.record_failure("yfinance", t + timedelta(minutes=4))       # third in 10 minutes
    assert health_monitor.is_down("yfinance", t + timedelta(minutes=10))
    assert not health_monitor.is_down("yfinance", t + timedelta(minutes=20))         # 15-minute cooldown over


def test_spread_out_failures_and_success_reset():
    t = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    health_monitor.record_failure("tavily", t)
    health_monitor.record_failure("tavily", t + timedelta(minutes=12))               # first one has aged out
    assert not health_monitor.record_failure("tavily", t + timedelta(minutes=13))
    health_monitor.record_success("tavily")
    assert not health_monitor.record_failure("tavily", t + timedelta(minutes=14))
    assert not health_monitor.is_down("trendlyne")                                   # not watched at all


def test_tracked_call_skips_a_source_marked_down(db):
    tc.set_quota_connection(db)
    calls = []

    async def fetch():
        calls.append(1)
        raise ConnectionError("down")

    async def run():
        for _ in range(3):
            with pytest.raises(tc.ToolCallFailed):
                await tc.tracked_call("frankfurter", "rate", fetch, retries=0)
        with pytest.raises(tc.ToolCallFailed) as err:
            await tc.tracked_call("frankfurter", "rate", fetch, retries=0)
        return err.value
    try:
        err = asyncio.run(run())
    finally:
        tc.set_quota_connection(None)
    assert len(calls) == 3 and isinstance(err.cause, tc.SourceDown)                  # the 4th call was never made


# --- Decision log -----------------------------------------------------------------------------

STATE = {
    "request_id": "r1", "raw_input": "should I invest in star health", "ticker": "STARHEALTH", "exchange": "NSE",
    "listing_currency": "INR", "verdict": "neutral", "confidence": "high", "disclaimer_version": "1",
    "user_profile": {"budget_amount": 1e6, "target_return_pct": 10.0},
    "scorecard": {"config_version": 2, "score": 0.10},
    "sizing": {"status": "ok", "amount": 39640.0, "currency": "INR"},
    "verified_records": [{"field": "current_price", "value": 551.0}],
    "projection": {"status": "ok", "years": [2027, 2028], "base_year": 2026, "flags": [],
                   "assumptions": {"growth": {"low": 0.115, "base": 0.138, "high": 0.161}},
                   "cases": {case: {2027: {"revenue": 2.0e11 * k, "eps": 15 * k}, 2028: {}}
                             for case, k in (("low", 0.9), ("base", 1.0), ("high", 1.1))}},
    "fair_value": {"status": "ok", "range": {"low": 586.0, "base": 605.0, "high": 630.0}},
    "report_path": "data/reports/STARHEALTH_x.pdf", "final_response": "Information only...",
}
RUN = {"request_id": "r1", "latency_s": 84.2, "cost": {"total_usd": 0.0412}, "timings": [], "calls": []}


def test_decision_log_entry_with_year1(db):
    assert decision_log.log_decision(STATE, run=RUN, conn=db) == "r1"
    row = decision_log.get("r1", db)
    assert row["verdict"] == "neutral" and row["recommended_amount"] == 39640.0 and row["config_version"] == "2"
    assert row["cost_usd"] == pytest.approx(0.0412) and row["latency_s"] == pytest.approx(84.2)
    y1 = row["payload"]["year1"]
    assert y1["fiscal_year"] == 2027 and y1["cases"]["base"]["eps"] == 15 and y1["price_at_analysis"] == 551.0
    assert y1["fair_value"] == {"low": 586.0, "base": 605.0, "high": 630.0}
    assert row["payload"]["profile"]["target_return_pct"] == 10.0
    assert decision_log.recent(5, db)[0]["ticker"] == "STARHEALTH"


def test_questions_are_not_logged(db):
    assert decision_log.log_decision({"request_id": "r2", "final_response": "Which company?"}, run=RUN, conn=db) is None
    assert decision_log.recent(5, db) == []


def test_cleanup_runs_at_most_once_a_month(db):
    assert decision_log.maybe_cleanup(db) is not None
    assert decision_log.maybe_cleanup(db) is None


# --- A whole request through main.run_turn ----------------------------------------------------

def test_run_turn_records_cost_timings_and_the_decision(monkeypatch, db):
    from invest_agent import main
    from invest_agent.observability.timings import timed as wrap

    class FakeGraph:
        async def ainvoke(self, state, config=None):
            async def analyse(s):
                await asyncio.sleep(0.01)
                return {}
            await wrap("analysis", analyse)(state)
            return {**STATE, "request_id": state["request_id"], "price_records": [1]}

    logged = []
    monkeypatch.setattr(main, "get_graph", lambda: FakeGraph())
    monkeypatch.setattr(main.decision_log, "log_decision", lambda result, run: logged.append((result, run)))
    monkeypatch.setattr(main.decision_log, "maybe_cleanup", lambda: None)
    monkeypatch.setattr(main.quotas, "warnings", lambda: ["Tavily: 850 of 1,000 credits used this month (85%)"])
    result, carry = asyncio.run(main.run_turn("should I invest in star health", {}))
    assert carry == {} and len(logged) == 1
    run = result["run"]
    assert run["timings"][0]["node"] == "analysis" and run["cost"]["total_usd"] == 0
    assert run["quota_warnings"] and run["latency_s"] >= 0
