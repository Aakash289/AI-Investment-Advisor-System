"""
main.py: run the agent from the terminal.

What this file does:
    Lets you talk to the agent in the terminal, one message at a time:

        uv run python -m invest_agent.main
        You: should I invest in reliance, I have 2 lakhs, conservative, 10 years
        Agent: ...

    or ask one question and exit:

        uv run python -m invest_agent.main "is apple a good buy"

    Each message runs the LangGraph graph once. When the agent ends a turn with a
    question (which company, missing profile details, a budget to confirm), the
    answers so far are carried into your next message, so you only answer the
    question; once a full answer is given, the next message starts fresh.

    Every request (step 14) also:
      keeps each MCP server open from its first call to the end of the request (A10)
      adds up the model tokens and prices them (observability/cost_tracker.py)
      times each step of the graph (observability/timings.py)
      saves the analysis to the decision log (observability/decision_log.py)
      prints any free-plan limit at 80% or more (observability/quotas.py)
    With --detail, the step timings and source health are printed too.

    The app (step 16, invest_agent/app) calls the same run_turn, with on_step for its
    live progress list and quiet=True so nothing is printed to the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid

# rich prints coloured, wrapped text in the terminal; Console is its main printer.
from rich.console import Console
from rich.markup import escape

from invest_agent.common.logging_setup import setup_logging
from invest_agent.common.records import utc_now_iso
from invest_agent.config import get_settings
from invest_agent.graph.build_graph import get_graph
from invest_agent.mcp_servers.clients import session_pool
from invest_agent.observability import decision_log, health_monitor, quotas
from invest_agent.observability.cost_tracker import (
    cost_line,
    track_model_usage,
    usage_summary,
)
from invest_agent.observability.timings import start_timings, timing_lines
from invest_agent.observability.tracked_call import start_call_log

console = Console()

# What is kept from a turn that ended with a question, so the next message continues it.
# user_profile is carried too, so a return target in the first message ("10% in the next
# 2 years") survives a question about which company was meant (it is not saved between
# sessions, so it existed only in the first turn's profile; found 6 October, Star Health).
CARRY_KEYS = ("entity_status", "entity_candidates", "entity_question", "ticker", "exchange",
              "listing_currency", "company_name", "market", "cik", "sic_code", "followup_asked",
              "user_profile")


async def run_turn(message: str, carry: dict, on_step=None, quiet: bool = False) -> tuple[dict, dict]:
    """Run the graph once. Returns (final state, what to carry into the next message).

    on_step(name, "start" or "done", seconds) is told as each graph step starts and ends
    (the app's progress list); quiet=True prints nothing (the app shows it instead)."""
    state = {**carry, "raw_input": message, "request_id": uuid.uuid4().hex, "started_at": utc_now_iso()}
    calls = start_call_log()                       # every outside call in this turn is recorded here
    timings = start_timings(on_step)               # how long each graph step takes
    t0 = time.perf_counter()

    # session_pool keeps each MCP server open for the whole request; track_model_usage
    # adds up the tokens of every model call inside the block.
    # recursion_limit is LangGraph's cap on steps in one run: the runaway protection
    # (MAX_GRAPH_STEPS, 50 by default).
    with track_model_usage() as usage:
        async with session_pool():
            result = await get_graph().ainvoke(state, config={"recursion_limit": get_settings().max_graph_steps})
    latency = time.perf_counter() - t0
    cost = usage_summary(usage.usage_metadata)

    ended_with_question = bool(result.get("final_response")) and not result.get("price_records") \
        and result.get("verdict") != "not_covered"
    next_carry = {k: result[k] for k in CARRY_KEYS if k in result} if ended_with_question else {}

    warnings = quotas.warnings()
    run = {"request_id": state["request_id"], "latency_s": round(latency, 1), "cost": cost, "timings": timings,
           "calls": calls, "quota_warnings": warnings}
    result["run"] = run
    result["ended_with_question"] = ended_with_question
    if not ended_with_question:
        decision_log.log_decision(result, run=run)
        decision_log.maybe_cleanup()

    if quiet:
        return result, next_carry
    console.print(f"[dim]({len(calls)} outside calls, {latency:.1f} s, {cost_line(cost)})[/dim]")
    for w in warnings:
        console.print(f"[yellow]Usage limit: {escape(w)}[/yellow]")
    return result, next_carry


async def interactive(first_message: str | None, detail: bool = False) -> None:
    carry: dict = {}
    message = first_message
    while True:
        if message is None:
            try:
                message = console.input("[bold cyan]You:[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message.lower() in {"", "exit", "quit"}:
                break
        result, carry = await run_turn(message, carry)
        if detail and result.get("preview_response"):
            console.print(f"[dim]{escape(result['preview_response'])}[/dim]\n")
        if detail and (result.get("run") or {}).get("timings"):
            lines = ["Step timings (start, duration, step):", *timing_lines(result["run"]["timings"]),
                     "Source health: " + ", ".join(f"{k} {v}" for k, v in health_monitor.snapshot().items())]
            console.print(f"[dim]{escape(chr(10).join(lines))}[/dim]\n")
        console.print(f"[bold green]Agent:[/bold green] {escape(result.get('final_response', ''))}\n")
        if first_message is not None and not carry:
            break                                  # one-question mode: stop once answered
        message = None


def main() -> None:
    # Windows terminals sometimes default to an older text encoding; UTF-8 makes the
    # rupee sign print correctly.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()

    # argparse reads the optional question typed after the command.
    parser = argparse.ArgumentParser(description="Investment Suitability Agent")
    parser.add_argument("message", nargs="?", help="ask one question and exit (leave out to chat)")
    parser.add_argument("--detail", action="store_true", help="also show the developer view of every step")
    args = parser.parse_args()

    console.print("[bold]Investment Suitability Agent[/bold]. Type 'exit' to stop.\n")
    asyncio.run(interactive(args.message, args.detail))


if __name__ == "__main__":
    main()
