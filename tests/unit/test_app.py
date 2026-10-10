"""
tests/unit/test_app.py: the Gradio app (build step 16).

What this file checks:
    progress     timed() tells the on_step callback when a step starts and ends, and a
                 failing callback never stops the step; the progress list groups the
                 graph's steps and names the retrieval agents still running
    the card     verdict badge, amount, score bars (a missing sub-score says so), the
                 return target and the backtest note; text is escaped; a run with no
                 analysis shows the chat answer instead
    the chat     the PDF's file path is replaced by a pointer to the download button
    the chart    drawn from the price history, none without one
    profile      the saved profile is shown and can be forgotten
    a full turn  ask() with a fake agent: progress first, then the card, chart and PDF;
                 a question carries its answers into the next message; an error is
                 shown in the chat without stopping the app
    the layout   build_app() builds

No network and no model: run_turn is replaced by a fake.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from invest_agent.app import ui, view
from invest_agent.observability.timings import start_timings, timed
from invest_agent.profile.store import ProfileStore
from invest_agent.storage.db import connect

from .test_report_integrity import state as report_state

# --- Progress ---------------------------------------------------------------------------------

def test_step_events_reach_the_callback():
    events = []

    async def node(state):
        return {"x": 1}

    async def go():
        start_timings(lambda name, event, secs: events.append((name, event)))
        return await timed("news", node)({})

    assert asyncio.run(go()) == {"x": 1}
    assert events == [("news", "start"), ("news", "done")]


def test_a_failing_callback_never_stops_a_step():
    def boom(*args):
        raise RuntimeError("display broke")

    def node(state):
        return {"ok": True}

    start_timings(boom)
    assert timed("sizing", node)({}) == {"ok": True}


def test_progress_groups_steps_and_names_what_is_still_running():
    steps = {"entity_resolution": {"state": "done", "secs": 2.0}, "profile_intake": {"state": "done", "secs": 3.0},
             "input_gate": {"state": "done", "secs": 0.0},
             "technical_price": {"state": "done", "secs": 4.0}, "news": {"state": "running", "secs": None},
             "macro": {"state": "running", "secs": None}}
    out = view.progress_html(steps, 31.2)
    assert "31 s so far" in out
    assert 'class="isa-step done"><span class="dot"></span>Understanding your question' in out
    assert "waiting for economy, news" in out
    assert 'class="isa-step "><span class="dot"></span>Writing the reasoning' in out


# --- The card ---------------------------------------------------------------------------------

def analysed() -> dict:
    s = report_state()
    s.update({"sub_scores": {"business_quality": 1.2, "valuation": -2.0, "macro_fit": 0.1, "news": None,
                             "forecast": -0.5},
              "scorecard": {"score": 0.21, "raw_verdict": "neutral", "weights_used": {
                  "business_quality": 0.53, "valuation": 0.21, "macro_fit": 0.16, "news": 0.0, "forecast": 0.1}},
              "company_name": "X <Ltd>"})
    return s


def test_card_shows_verdict_amount_and_bars():
    out = view.card_html(analysed())
    assert 'isa-badge isa-neutral">Neutral<' in out
    assert "Confidence: low" in out and "None suggested" in out
    assert "Business quality" in out and "+1.20" in out and "-2.00" in out
    assert 'isa-missing">missing<' in out                 # news had no score
    assert "Weighted score" in out and "+0.21" in out
    assert "X &lt;Ltd&gt;" in out and "<Ltd>" not in out  # escaped
    assert "The P/E is 21.5." in out                     # risk shown without its source mark


def test_card_without_an_analysis_shows_the_answer():
    out = view.card_html({"verdict": "not_covered", "final_response": "Not covered: a REIT."})
    assert "No analysis this time" in out and "Not covered: a REIT." in out


def test_bar_widths():
    assert "left:50.0%;width:25.0%" in view._bar(1.0)       # +1 fills a quarter of the track
    assert "left:0.0%;width:50.0%" in view._bar(-5)         # clipped at -2
    assert "fill" not in view._bar(None)


def test_chat_text_hides_the_file_path():
    r = {"final_response": "Verdict.\n\nFull report (PDF): C:\\data\\reports\\X.pdf\n\nDisclaimer: text",
         "report_path": "C:\\data\\reports\\X.pdf"}
    out = view.chat_text(r)
    assert "C:\\" not in out and "download the PDF" in out and out.endswith("Disclaimer: text")


def test_chart_image():
    start = date(2021, 10, 1)
    chart = {"dates": [(start + timedelta(weeks=i)).isoformat() for i in range(60)],
             "stock": [100 + i for i in range(60)], "benchmark": [50 + i / 2 for i in range(60)],
             "benchmark_symbol": "^NSEI"}
    img = view.chart_image({"price_chart": chart, "listing_currency": "INR"})
    assert img is not None and img.size[0] > 500
    assert view.chart_image({}) is None


# --- Profile ----------------------------------------------------------------------------------

def test_profile_shown_and_forgotten():
    store = ProfileStore(connect(":memory:"))
    assert "Nothing saved yet" in view.profile_html(store.load(), store.updated_at())
    store.save({"budget_amount": 200000, "budget_currency": "INR", "risk_tolerance": "low", "horizon_years": 10,
                "goal": "growth"})
    out = view.profile_html(store.load(), store.updated_at())
    assert "₹2,00,000" in out and "10 years" in out and "not given" in out and "Last saved" in out
    assert store.clear() is True and store.load() == {} and store.clear() is False


# --- A full turn through the app ----------------------------------------------------------------

def _drain(gen):
    return list(gen)


def test_ask_shows_progress_then_the_result(monkeypatch, tmp_path):
    pdf = tmp_path / "X.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = {**analysed(), "report_path": str(pdf), "ended_with_question": False,
              "run": {"latency_s": 1.0, "cost": {"total_usd": 0.01}, "calls": [], "timings": []}}

    async def fake_run_turn(message, carry, on_step=None, quiet=False):
        assert quiet is True
        on_step("entity_resolution", "start", None)
        await asyncio.sleep(0.7)
        on_step("entity_resolution", "done", 0.7)
        return result, {}

    monkeypatch.setattr(ui, "run_turn", fake_run_turn)
    frames = _drain(ui.ask("is X a good buy", [], {}))
    first, last = frames[0], frames[-1]
    assert first[0][-1]["content"].startswith("Working on it") and first[1] == ""
    assert "Working on it" in first[4]                      # progress list in the card
    assert any("isa-step running" in f[4] for f in frames[1:-1] if isinstance(f[4], str))
    chat, _, carry, run, card, _chart, file, behind = last
    assert chat[-1]["role"] == "assistant" and "Body" in chat[-1]["content"]
    assert carry == {} and run["latency_s"] == 1.0
    assert "isa-neutral" in card and "Last run" in behind
    assert file["value"] == str(pdf) and file["visible"] is True


def test_a_question_carries_into_the_next_message(monkeypatch):
    async def fake_run_turn(message, carry, on_step=None, quiet=False):
        return ({"final_response": "Which one: Reliance Industries (NSE) or Reliance, Inc. (NYSE)?",
                 "entity_question": "Which one: Reliance Industries (NSE) or Reliance, Inc. (NYSE)?",
                 "ended_with_question": True}, {"entity_status": "ask_user"})

    monkeypatch.setattr(ui, "run_turn", fake_run_turn)
    last = _drain(ui.ask("reliance", [], {}))[-1]
    assert last[2] == {"entity_status": "ask_user"}
    assert "Waiting for your answer" in last[4] and "Reliance, Inc." in last[4]


def test_an_error_is_shown_in_the_chat(monkeypatch):
    async def broken(message, carry, on_step=None, quiet=False):
        raise ValueError("bad thing")

    monkeypatch.setattr(ui, "run_turn", broken)
    last = _drain(ui.ask("apple", [], {"x": 1}))[-1]
    assert "Something went wrong" in last[0][-1]["content"] and "bad thing" in last[0][-1]["content"]
    assert last[2] == {}                                     # the half-finished carry is dropped


def test_empty_message_does_nothing():
    frames = _drain(ui.ask("   ", [], {}))
    assert len(frames) == 1


def test_layout_builds():
    pytest.importorskip("gradio")
    assert ui.build_app() is not None
