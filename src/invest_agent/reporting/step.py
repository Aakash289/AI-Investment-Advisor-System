"""
reporting/step.py: the Report node, the last step of a request (design 14.14).

What this file does:
    After compliance has written the answer, this builds the report data object
    (report_data.py), draws and saves the PDF (render.py), and adds one line to the
    answer saying where the report is, just above the disclaimer, which stays last.

    No PDF for a company not covered (there is no analysis to report). If anything here
    fails, the answer is still shown, with a line saying the report could not be
    produced, and the error is kept in the state for the decision log.
"""

from __future__ import annotations

import logging

from invest_agent.config import get_settings
from invest_agent.graph.state import GraphState
from invest_agent.reporting import render, report_data

logger = logging.getLogger(__name__)


def _insert_line(answer: str, line: str) -> str:
    """Put a line just above the disclaimer, which must stay at the end."""
    marker = "\n\nDisclaimer: "
    i = answer.rfind(marker)
    return answer if i < 0 else answer[:i] + f"\n\n{line}" + answer[i:]


def report_node(state: GraphState) -> dict:
    answer = state.get("final_response") or ""
    if state.get("verdict") == "not_covered" or not state.get("disclaimer_version"):
        return {}
    try:
        data = report_data.build(state)
        path = render.save(data, get_settings().reports_dir, state.get("ticker") or "report")
    except Exception as err:
        logger.warning("Report failed: %s", err)
        return {"report_error": f"{type(err).__name__}: {err}",
                "final_response": _insert_line(answer, "The full PDF report could not be produced this time.")}
    return {"report_path": path, "final_response": _insert_line(answer, f"Full report (PDF): {path}")}
