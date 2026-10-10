"""
tests/unit/test_spread_check.py: the verdict spread check (eval/spread_check.py).

What this file checks:
    the list      the company list loads and has both markets
    the report    verdict counts, the scores in order, how many sit inside the neutral band,
                  each sub-score's pull, and the what-ifs (lines at +-0.5, without economic
                  fit and news) on made-up rows
    the log       rows are read back from decision log entries
    the run       with a fake agent: each company asked once with the fixed profile, a
                  listing question answered once, the batch saved so a second run skips
                  what is done, and the saved profile put back

No network and no model.
"""

from __future__ import annotations

import asyncio
import json

from invest_agent import main as main_module
from invest_agent.eval import spread_check
from invest_agent.observability import decision_log
from invest_agent.profile.store import ProfileStore
from invest_agent.storage.db import connect


def row(ticker, score, verdict="neutral", **subs):
    base = {"business_quality": 0.0, "valuation": 0.0, "macro_fit": 0.0, "news": 0.0, "forecast": None}
    base.update(subs)
    return {"ticker": ticker, "market": "IN", "verdict": verdict, "confidence": "medium", "score": score,
            "subs": base, "weights": {"business_quality": 0.56, "valuation": 0.22, "macro_fit": 0.17, "news": 0.05},
            "low_reliability": [], "capped": False, "config_version": "4"}


def test_company_list():
    cfg = spread_check.companies()
    queries = [c["query"] for c in cfg["companies"]]
    assert len(queries) >= 20 and any("NSE" in q for q in queries) and any("NYSE" in q for q in queries)
    assert "goal growth" in cfg["profile_text"]


def test_report_and_what_ifs():
    rows = [row("AAA", 0.62, business_quality=1.5, valuation=0.5, macro_fit=-0.5, news=0.0),
            row("BBB", -0.10, business_quality=0.4, valuation=-1.0),
            row("CCC", -0.90, "unfavourable", business_quality=-1.5, valuation=-0.5)]
    rep = spread_check.report(rows)
    assert rep["verdicts"] == {"neutral": 2, "unfavourable": 1}
    assert rep["scores"] == [-0.9, -0.1, 0.62] and rep["inside_neutral_band"] == 2
    assert rep["pull"]["forecast"]["missing"] == 3 and rep["pull"]["macro_fit"]["near_zero"] == 2
    assert rep["what_if"]["lines at +-0.5"] == {"favourable": 1, "neutral": 1, "unfavourable": 1}
    # Without economic fit and news, AAA's +1.5 and +0.5 are no longer diluted by -0.5 and 0.
    assert rep["what_if"]["without economic fit and news"]["favourable"] == 1
    lines = spread_check.text(rows, rep)
    assert any(line.startswith("Verdicts: neutral 2, unfavourable 1") for line in lines)
    assert any("inside the neutral band" in line for line in lines)


def test_rows_from_log(monkeypatch):
    conn = connect(":memory:")
    monkeypatch.setattr(spread_check, "connect", lambda: conn)
    state = {"request_id": "r1", "ticker": "TCS", "market": "IN", "verdict": "neutral", "confidence": "low",
             "sub_scores": {"business_quality": 1.0, "valuation": -2.0},
             "scorecard": {"score": 0.1, "weights_used": {"business_quality": 0.7, "valuation": 0.3},
                           "low_reliability": ["valuation"], "config_version": 4}}
    decision_log.log_decision(state, run={"request_id": "r1"}, conn=conn)
    rows = spread_check.rows_from_log(["r1", None])
    assert rows[0]["ticker"] == "TCS" and rows[0]["subs"]["valuation"] == -2.0
    assert rows[0]["low_reliability"] == ["valuation"]


def test_run_asks_each_company_once_and_restores_the_profile(monkeypatch, tmp_path):
    store = ProfileStore(connect(":memory:"))
    store.save({"budget_amount": 200000, "budget_currency": "INR", "risk_tolerance": "low"})
    monkeypatch.setattr(spread_check, "ProfileStore", lambda: store)
    monkeypatch.setattr(spread_check, "batch_path", lambda version: tmp_path / "spread.json")
    monkeypatch.setattr(spread_check, "companies", lambda: {
        "version": 1, "profile_text": "I have 5 lakhs, medium risk, 10 years, goal growth",
        "companies": [{"query": "TCS on NSE"}, {"query": "Reliance on NSE", "reply": "the NSE one"}]})
    asked = []

    async def fake_run_turn(message, carry, on_step=None, quiet=False):
        asked.append(message)
        store.save({"budget_amount": 500000, "budget_currency": "INR", "risk_tolerance": "medium"})
        if message.startswith("Should I invest in Reliance"):
            return {"ended_with_question": True, "final_response": "Which Reliance?"}, {"entity_status": "ask_user"}
        return ({"verdict": "neutral", "confidence": "low", "scorecard": {"score": 0.1},
                 "run": {"request_id": f"id{len(asked)}", "latency_s": 1.0}}, {})

    monkeypatch.setattr(main_module, "run_turn", fake_run_turn)
    batch = asyncio.run(spread_check.run_all())
    assert asked[0] == "Should I invest in TCS on NSE? I have 5 lakhs, medium risk, 10 years, goal growth"
    assert asked[2] == "the NSE one"                          # the listing question answered once
    assert set(batch) == {"TCS on NSE", "Reliance on NSE"}
    assert store.load()["risk_tolerance"] == "low"            # your profile put back

    asked.clear()
    asyncio.run(spread_check.run_all())
    assert asked == []                                        # nothing left to run
    assert json.loads((tmp_path / "spread.json").read_text())["TCS on NSE"]["request_id"] == "id1"
