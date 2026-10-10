"""
observability/tracked_call.py: the one wrapper every outside call goes through.

What this file does:
    Every call to an outside service (SEC, yfinance, Trendlyne, Tavily, FRED, an MCP
    tool, and so on) is made as

        result = await tracked_call("sec_edgar", "company_facts", fetch_facts, cik)

    instead of calling fetch_facts(cik) directly. The wrapper then, in one place:
      1. waits if the service's rate limit needs a gap since the previous call
         (SEC under 10 a second, Massive 5 a minute);
      2. applies the timeout (10 seconds) and retries (2, with a growing wait)
         decided on 4 October, before the caller moves to its fallback source;
      3. counts quota units in SQLite for today and this month;
      4. records a CallRecord (service, duration, attempts, success) in the current
         request's call log, which the decision log and cost tracker read later;
      5. tells the Source Health Monitor whether the call worked, and fails at once,
         without calling, when the monitor has marked the service down (step 14).

    It works for both kinds of function: `async def` functions (httpx, MCP tools) are
    awaited, and ordinary functions (yfinance) are run in a worker thread so they do
    not block the other retrieval agents running in parallel.

Design references: 14.16 (measurement, rate limits by waiting), 14.3 (timeout and
retries), 15.19.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

# httpx is the HTTP client used for every REST API in the app. Its HTTPStatusError
# carries the response status code, which decides whether a retry makes sense.
import httpx

# tenacity is a retry library. AsyncRetrying runs a block several times:
#   stop_after_attempt(n)        give up after n attempts in total
#   wait_exponential(...)        wait 0.5 s, then 1 s, then 2 s ... between attempts
#   retry_if_exception(pred)     only retry when pred(error) is True
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from invest_agent.common.records import utc_now_iso
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.graph.state import CallRecord
from invest_agent.observability import health_monitor
from invest_agent.storage.db import add_quota_units, connect

logger = logging.getLogger(__name__)


class SourceDown(RuntimeError):
    """The Source Health Monitor has marked the service down; the call was not made."""


class ToolCallFailed(RuntimeError):
    """Raised when a call still fails after its retries. The caller catches this and
    moves to the fallback source (the Source Health Monitor's job, step 14)."""

    def __init__(self, service: str, operation: str, cause: BaseException) -> None:
        super().__init__(f"{service}.{operation} failed: {cause!r}")
        self.service = service
        self.operation = operation
        self.cause = cause


# --- Request-scoped call log ----------------------------------------------------------
# contextvars gives each running request its own copy of a variable, even when
# several requests or parallel agents share one process and event loop. Each request
# calls start_call_log() at the beginning; every tracked_call inside that request
# appends to the same list without the list being passed around.

_call_log: contextvars.ContextVar[list[CallRecord] | None] = contextvars.ContextVar("call_log", default=None)


def start_call_log() -> list[CallRecord]:
    """Begin a fresh call log for the current request and return it."""
    log: list[CallRecord] = []
    _call_log.set(log)
    return log


def current_call_log() -> list[CallRecord]:
    """The current request's call log (empty if no request has started one)."""
    return _call_log.get() or []


# --- Rate limiting by waiting ----------------------------------------------------------

_last_call_at: dict[str, float] = {}       # service -> time.monotonic() of the last call
_locks: dict[str, asyncio.Lock] = {}       # one lock per service so waits queue in order


def _service_limits(service: str) -> dict[str, Any]:
    return load_yaml("observability/pricing.yaml").get("services", {}).get(service, {}) or {}


async def _wait_for_rate_limit(service: str) -> None:
    """Sleep just long enough to respect the service's minimum gap between calls."""
    interval = float(_service_limits(service).get("min_interval_seconds", 0))
    if interval <= 0:
        return
    lock = _locks.setdefault(service, asyncio.Lock())
    async with lock:
        # time.monotonic() is a clock that never jumps backwards, unlike wall time.
        elapsed = time.monotonic() - _last_call_at.get(service, 0.0)
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        _last_call_at[service] = time.monotonic()


# --- Quota counting ----------------------------------------------------------------------

_quota_conn: sqlite3.Connection | None = None


def set_quota_connection(conn: sqlite3.Connection | None) -> None:
    """Use a specific database connection for quota counts (tests pass an in-memory one)."""
    global _quota_conn
    _quota_conn = conn


def _record_quota(service: str, units: int) -> None:
    """Add quota units; a database problem is logged, never allowed to fail the call."""
    global _quota_conn
    if units <= 0:
        return
    try:
        if _quota_conn is None:
            _quota_conn = connect()
        add_quota_units(_quota_conn, service, units)
    except sqlite3.Error as err:
        logger.warning("Could not record quota for %s: %s", service, err)


# --- Which errors are worth retrying ---------------------------------------------------------

def _is_retryable(err: BaseException) -> bool:
    """Retry timeouts, connection problems, rate limiting (429) and server errors (5xx).
    Do not retry other client errors (400, 401, 403, 404): repeating the same bad
    request cannot succeed and only wastes quota."""
    if isinstance(err, httpx.HTTPStatusError):
        code = err.response.status_code
        return code == 429 or code >= 500
    return isinstance(err, (asyncio.TimeoutError, TimeoutError, httpx.TransportError, ConnectionError))


# --- The wrapper ---------------------------------------------------------------------------------

async def tracked_call(
    service: str,
    operation: str,
    fn: Callable[..., Any],
    *args: Any,
    quota_units: int = 1,
    timeout: float | None = None,
    retries: int | None = None,
    **kwargs: Any,
) -> Any:
    """Call fn(*args, **kwargs) with rate limiting, timeout, retries, quota counting
    and logging. Returns fn's result or raises ToolCallFailed.

    service:      key in pricing.yaml and in the quota table, e.g. "sec_edgar"
    operation:    short label for the log, e.g. "company_facts"
    quota_units:  units this call uses on success (Tavily: credits; most: 1)
    timeout, retries: default to TOOL_TIMEOUT_SECONDS and TOOL_RETRIES from .env
    """
    settings = get_settings()
    timeout = settings.tool_timeout_seconds if timeout is None else timeout
    retries = settings.tool_retries if retries is None else retries

    started_at = utc_now_iso()
    t0 = time.perf_counter()          # perf_counter is the most precise timer for durations
    attempts = 0

    # Source Health Monitor (step 14): a source that kept failing is skipped for a while,
    # so the caller moves to its fallback at once instead of waiting out the timeouts.
    if health_monitor.is_down(service):
        err = SourceDown(f"{service} is marked down after repeated failures")
        _log_call(service, operation, started_at, t0, ok=False, attempts=0, units=0, error=repr(err))
        raise ToolCallFailed(service, operation, err)

    async def _one_attempt() -> Any:
        nonlocal attempts
        attempts += 1
        await _wait_for_rate_limit(service)
        if inspect.iscoroutinefunction(fn):
            # async function: run it, but stop waiting after `timeout` seconds
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
        # ordinary function: asyncio.to_thread runs it in a worker thread so the event
        # loop (and the other parallel agents) keep running while it waits on the network
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)

    try:
        result = None
        # "async for attempt in AsyncRetrying(...)" is tenacity's loop form: the body
        # runs once per attempt, and tenacity decides whether to wait and go again.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(retries + 1),
            wait=wait_exponential(multiplier=0.5, max=4),
            retry=retry_if_exception(_is_retryable),
            reraise=True,             # after the last attempt, raise the real error
        ):
            with attempt:
                result = await _one_attempt()
    except Exception as err:
        _log_call(service, operation, started_at, t0, ok=False, attempts=attempts, units=0, error=repr(err))
        health_monitor.record_failure(service)
        raise ToolCallFailed(service, operation, err) from err

    health_monitor.record_success(service)
    _record_quota(service, quota_units)
    _log_call(service, operation, started_at, t0, ok=True, attempts=attempts, units=quota_units)
    return result


def _log_call(service: str, operation: str, started_at: str, t0: float, *, ok: bool,
              attempts: int, units: int, error: str | None = None) -> None:
    """Append a CallRecord to the request's call log and write a debug line."""
    record: CallRecord = {
        "service": service,
        "operation": operation,
        "started_at": started_at,
        "duration_s": round(time.perf_counter() - t0, 3),
        "ok": ok,
        "attempts": attempts,
        "quota_units": units,
        "cost_usd": 0.0,              # outside data calls are free; model cost is added by the cost tracker
    }
    if error:
        record["error"] = error
    log = _call_log.get()
    if log is not None:
        log.append(record)
    logger.debug("call %s.%s ok=%s attempts=%s %.2fs", service, operation, ok, attempts, record["duration_s"])
