"""
observability/timings.py: how long each step of the graph took in one request (A10).

What this file does:
    A request takes 30 to 80 seconds, and the design asks which steps the time goes to
    before anything is made faster (A10). build_graph wraps every node with timed(),
    which records the node's name, start offset and duration in the current request's
    list:

        timings = start_timings()
        result = await graph.ainvoke(state)
        print(slowest(timings))     # [("news", 21.4), ("synthesis", 14.2), ...]

    Nodes that run in parallel (the six retrieval agents) each get their own entry, so
    the list shows both what was slow and what overlapped. The list is kept in the
    decision log and shown in the developer view (--detail).

    Uses a context variable, like the call log in tracked_call.py, so parallel nodes
    and several requests in one process each see their own list.

    Step progress (step 16, the app): start_timings(on_step=...) also takes a function
    that is told when each step starts and ends, on_step(name, "start", None) and
    on_step(name, "done", seconds). The app uses it for its live progress list, so a
    two-minute run is not a silent wait. A failing callback never stops the request.
"""

from __future__ import annotations

import contextvars
import inspect
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)

_timings: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar("node_timings", default=None)
_start: contextvars.ContextVar[float] = contextvars.ContextVar("request_start", default=0.0)
_on_step: contextvars.ContextVar[Callable[[str, str, float | None], None] | None] = \
    contextvars.ContextVar("on_step", default=None)


def start_timings(on_step: Callable[[str, str, float | None], None] | None = None) -> list[dict]:
    """Begin a fresh timing list for the current request and return it. on_step, if
    given, is told as each step starts and finishes (the app's progress list)."""
    log: list[dict] = []
    _timings.set(log)
    _start.set(time.perf_counter())
    _on_step.set(on_step)
    return log


def _notify(name: str, event: str, seconds: float | None) -> None:
    callback = _on_step.get()
    if callback is None:
        return
    try:
        callback(name, event, seconds)
    except Exception as err:                # noqa: BLE001 - progress display must never break a request
        logger.debug("Progress callback failed for %s: %s", name, err)


def _record(name: str, t0: float) -> None:
    duration = round(time.perf_counter() - t0, 2)
    log = _timings.get()
    if log is not None:
        log.append({"node": name, "start_s": round(t0 - _start.get(), 2), "duration_s": duration})
    _notify(name, "done", duration)


def timed(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a graph node so its duration is recorded. Keeps the node async if it was."""
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def run_async(state, *args, **kwargs):
            t0 = time.perf_counter()
            _notify(name, "start", None)
            try:
                return await fn(state, *args, **kwargs)
            finally:
                _record(name, t0)
        return run_async

    @wraps(fn)
    def run(state, *args, **kwargs):
        t0 = time.perf_counter()
        _notify(name, "start", None)
        try:
            return fn(state, *args, **kwargs)
        finally:
            _record(name, t0)
    return run


def slowest(timings: list[dict], n: int = 5) -> list[tuple[str, float]]:
    """The n longest steps, longest first."""
    return [(t["node"], t["duration_s"]) for t in sorted(timings, key=lambda t: -t["duration_s"])[:n]]


def timing_lines(timings: list[dict]) -> list[str]:
    """The developer view's step timing table, in the order the steps started."""
    return [f"  {t['start_s']:6.1f}s  +{t['duration_s']:5.1f}s  {t['node']}"
            for t in sorted(timings, key=lambda t: t["start_s"])]
