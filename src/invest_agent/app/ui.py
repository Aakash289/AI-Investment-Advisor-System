"""
app/ui.py: the Gradio app (build step 16).

What this file does:
    Puts the agent in a browser window on your own computer:

        uv run python -m invest_agent.app

    opens http://127.0.0.1:7860. It is for one user on this computer only: it listens
    on 127.0.0.1, so no other device can reach it, and no public link is made (a public
    link would let anyone spend the OpenAI credit and the Trendlyne quota).

    Four tabs:
      Analysis            the chat on the left; on the right the live progress list
                          while a run goes, then the result card, the five-year price
                          chart and the PDF to download. The short disclaimer is always
                          shown under them, the full one a click away.
      Your profile        what the agent has saved (budget, risk, horizon, goal), with a
                          button to forget it
      Behind the scenes   the last run's time, model cost and step timings, source
                          health and free-plan usage (what --detail prints in the terminal)
      Track record        the latest analyses from the decision log and the replay summary
                          (the list and the profile are read again after every run)

    Times are shown in this computer's own time zone; the database keeps UTC.

    The chat is the only way in, as the design decided (one plain-language box, no form):
    follow-up questions (which listing, missing profile details) are answered in the
    chat, and the answers so far are carried into the next message exactly as in the
    terminal (main.run_turn and its carry).

How a run works:
    The agent is asynchronous and starts MCP server processes. All runs go to one
    background event loop that lives as long as the app (Runner below), so the same
    loop is used every time; on Windows it is a ProactorEventLoop, the kind that can start
    subprocesses. The Gradio handler starts the run there, then reads the step events
    (observability/timings.py on_step) from a queue and redraws the progress list twice
    a second until the run finishes. Only one run at a time (concurrency_limit=1).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import sys
import threading
import time

# gradio: the web app library. Blocks lays out components; events connect them to functions.
import gradio as gr

from invest_agent.app import view
from invest_agent.app.theme import CSS, THEME
from invest_agent.common.logging_setup import setup_logging
from invest_agent.compliance.disclaimer import config as disclaimer_config
from invest_agent.config import get_settings
from invest_agent.main import run_turn
from invest_agent.observability import decision_log, health_monitor, quotas
from invest_agent.profile.store import ProfileStore

EXAMPLES = [
    "Should I invest in Reliance Industries? I have 2 lakhs, fairly conservative, 10 years, growth",
    "Is Apple a good buy for me?",
    "Star Health Insurance, I want 10% returns in the next 2 years",
    "Should I buy Infosys? I may need the money in 2 years",
]


# --- The background event loop -----------------------------------------------------------------

class Runner:
    """One event loop in its own thread, kept for the life of the app."""

    def __init__(self) -> None:
        # The Proactor loop is the Windows loop that supports subprocesses (the MCP servers).
        self.loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="agent-loop", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


_runner: Runner | None = None


def runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner()
    return _runner


# --- Event handlers -----------------------------------------------------------------------------

def _side_panels(run: dict | None) -> str:
    try:
        return view.run_html({"run": run} if run else None, health_monitor.snapshot(), quotas.status())
    except Exception as err:                    # noqa: BLE001
        return f'<div class="isa-note">Could not read the run details: {type(err).__name__}</div>'


def ask(message: str, history: list[dict], carry: dict):
    """Run one turn. A generator: each yield redraws the screen (progress, then the result).
    Outputs: chat, input box, carry, last run, card, chart, PDF, behind-the-scenes panel."""
    message = (message or "").strip()
    if not message:
        yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
        return
    history = [*(history or []), {"role": "user", "content": message},
               {"role": "assistant", "content": "Working on it…"}]

    events: queue.Queue = queue.Queue()
    steps: dict[str, dict] = {}

    def on_step(name: str, event: str, seconds: float | None) -> None:
        events.put((name, event, seconds))        # called on the agent's loop thread; Queue is thread-safe

    t0 = time.perf_counter()
    future = runner().submit(run_turn(message, dict(carry or {}), on_step=on_step, quiet=True))
    yield (history, "", gr.skip(), gr.skip(), view.progress_html(steps, 0), gr.update(visible=False),
           gr.update(value=None, visible=False), gr.skip())

    while True:
        try:
            future.result(timeout=0.5)
            finished = True
        except concurrent.futures.TimeoutError:
            finished = False
        except Exception:                        # noqa: BLE001 - shown below
            finished = True
        while not events.empty():
            name, event, seconds = events.get_nowait()
            steps[name] = {"state": "running" if event == "start" else "done", "secs": seconds}
        if finished:
            break
        yield (gr.skip(), gr.skip(), gr.skip(), gr.skip(), view.progress_html(steps, time.perf_counter() - t0),
               gr.skip(), gr.skip(), gr.skip())

    try:
        result, next_carry = future.result()
    except Exception as err:                     # noqa: BLE001 - one failed run must not stop the app
        history = history[:-1] + [{"role": "assistant",
                                   "content": f"Something went wrong and no answer was produced ({type(err).__name__}: {err}). "
                                              "Please try again; if it keeps happening, the terminal "
                                              "shows the details."}]
        yield history, gr.skip(), {}, gr.skip(), view.EMPTY, gr.skip(), gr.skip(), gr.skip()
        return

    # A new list each time: Gradio may still hold the one yielded with "Working on it…".
    history = history[:-1] + [{"role": "assistant", "content": view.chat_text(result)}]
    if result.get("ended_with_question"):
        question = result.get("entity_question") or result.get("followup_question") or result.get("final_response", "")
        yield (history, gr.skip(), next_carry, gr.skip(), view.question_html(question),
               gr.update(visible=False), gr.update(value=None, visible=False), gr.skip())
        return

    image = view.chart_image(result)
    pdf = result.get("report_path")
    run = result.get("run")
    yield (history, gr.skip(), next_carry, run, view.card_html(result),
           gr.update(value=image, visible=image is not None), gr.update(value=pdf, visible=bool(pdf)),
           _side_panels(run))


def new_conversation():
    return [], "", {}, view.EMPTY, gr.update(value=None, visible=False), gr.update(value=None, visible=False)


def show_profile():
    store = ProfileStore()
    return view.profile_html(store.load(), store.updated_at())


def forget_profile():
    ProfileStore().clear()
    return show_profile(), gr.update(visible=False)


def recent_decisions() -> str:
    try:
        return view.decisions_html(decision_log.recent(20))
    except Exception as err:                    # noqa: BLE001
        return f'<div class="isa-note">Could not read the decision log: {type(err).__name__}</div>'


def after_run() -> tuple[str, str]:
    """The decision log list and the profile, read again once a run has finished."""
    return recent_decisions(), show_profile()


def track_record() -> tuple[str, str]:
    decisions = recent_decisions()
    try:
        from invest_agent.eval import replay
        from invest_agent.storage.db import connect
        replay_text = "\n".join(replay.text(replay.report(connect())))
    except Exception as err:                    # noqa: BLE001
        replay_text = f"No replay results to show ({type(err).__name__})."
    return decisions, replay_text


# --- Layout -------------------------------------------------------------------------------------

def build_app() -> gr.Blocks:
    disclaimer = disclaimer_config()
    with gr.Blocks(title="Investment Suitability Agent") as app:
        carry = gr.State({})                     # what a question carries into the next message
        last_run = gr.State(None)                # the last run's details, for the behind-the-scenes tab

        gr.HTML('<div class="isa-header"><h1>Investment Suitability Agent</h1>'
                '<span>Is this stock a sensible fit for you, and how much would make sense?</span></div>')

        with gr.Tabs():
            with gr.Tab("Analysis"):
                with gr.Row(equal_height=False):
                    with gr.Column(scale=5, min_width=380):
                        chat = gr.Chatbot(height=600, show_label=False, buttons=["copy"], layout="bubble",
                                          placeholder="Ask about a company listed in India or the US.")
                        with gr.Row():
                            box = gr.Textbox(placeholder="e.g. Should I invest in Titan? I have 3 lakhs, 5 years",
                                             show_label=False, lines=1, max_lines=4, scale=8, autofocus=True)
                            send = gr.Button("Ask", variant="primary", scale=1, min_width=80)
                        gr.Examples(EXAMPLES, inputs=box, label="Try one")
                        reset = gr.Button("New conversation", variant="secondary", size="sm")
                    with gr.Column(scale=6, min_width=420):
                        card = gr.HTML(view.EMPTY)
                        # The chart carries its own title (dates and index), so no label over it.
                        chart = gr.Image(show_label=False, interactive=False, visible=False,
                                         buttons=["download"], type="pil")
                        pdf = gr.File(label="Full report (PDF)", interactive=False, visible=False)
                gr.HTML(f'<div class="isa-disclaimer">{view.E(disclaimer.get("short") or "")}</div>')
                with gr.Accordion("Full disclaimer", open=False):
                    gr.Markdown(disclaimer.get("full") or "")

            with gr.Tab("Your profile"):
                profile = gr.HTML()
                with gr.Row():
                    refresh_profile = gr.Button("Refresh", size="sm")
                    forget = gr.Button("Forget my saved profile", variant="stop", size="sm")
                    confirm = gr.Button("Yes, forget it", variant="stop", size="sm", visible=False)

            with gr.Tab("Behind the scenes"):
                behind = gr.HTML(_side_panels(None))
                refresh_behind = gr.Button("Refresh", size="sm")

            with gr.Tab("Track record"):
                gr.Markdown("The latest analyses, and how this scorecard did when replayed on past dates "
                            "(US companies, 2022 to 2025).")
                decisions = gr.HTML()
                replay_box = gr.Textbox(label="Replay summary", lines=18, interactive=False)
                refresh_track = gr.Button("Refresh", size="sm")

        outputs = [chat, box, carry, last_run, card, chart, pdf, behind]
        # concurrency_limit=1: one analysis at a time, since they share the quotas and servers.
        # After each run the profile and the decision log list are read again, so the other
        # tabs show the new analysis without pressing Refresh (found 7 October, Eternal).
        send.click(ask, [box, chat, carry], outputs, concurrency_limit=1).then(after_run, None, [decisions, profile])
        box.submit(ask, [box, chat, carry], outputs, concurrency_limit=1).then(after_run, None, [decisions, profile])
        reset.click(new_conversation, None, [chat, box, carry, card, chart, pdf])

        app.load(show_profile, None, profile)
        refresh_profile.click(show_profile, None, profile)
        forget.click(lambda: gr.update(visible=True), None, confirm)
        confirm.click(forget_profile, None, [profile, confirm])
        refresh_behind.click(_side_panels, last_run, behind)
        app.load(track_record, None, [decisions, replay_box])
        refresh_track.click(track_record, None, [decisions, replay_box])
    return app


def main() -> None:
    # Windows terminals sometimes default to an older text encoding; UTF-8 keeps the rupee sign.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    reports = get_settings().reports_dir
    # 127.0.0.1: this computer only. allowed_paths lets the app hand out the PDFs it made.
    build_app().launch(server_name="127.0.0.1", server_port=7860, inbrowser=True, share=False,
                       theme=THEME, css=CSS, allowed_paths=[reports], footer_links=[])


if __name__ == "__main__":
    main()
