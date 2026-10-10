"""
observability/health_monitor.py: the Source Health Monitor (design 7, 14.3, 14.16).

What this file does:
    When an outside source is down, every call to it still waits for the 10-second
    timeout and two retries (up to about 35 seconds) before the caller moves to its
    fallback. If the source stays down, every request pays that wait again.

    The monitor remembers failures. tracked_call.py tells it about each call:

      record_failure(service)   a call that still failed after its retries
      record_success(service)   a call that worked: the failure count starts again

    After `failures` failed calls within `window_minutes`, the source is marked down
    for `cooldown_minutes`. While it is down, is_down(service) is True and tracked_call
    fails the call at once, so the caller goes straight to its fallback (yfinance to
    Massive, SEC to FMP, Frankfurter to FRED, Tavily to GDELT). When the cooldown ends,
    the next call tries the source again.

    Only services with a "health" entry in observability/pricing.yaml are watched, and
    the settings live there:
        health: {failures: 3, window_minutes: 10, cooldown_minutes: 15}

    The state is kept in the shared cache table (namespace "source_health"), so the MCP
    server processes and the main app see the same picture. A database problem never
    fails a call: the monitor then simply says the source is up.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from invest_agent.cache.store import CacheStore
from invest_agent.common.yaml_loader import load_yaml

logger = logging.getLogger(__name__)
NAMESPACE = "source_health"

_store: CacheStore | None = None


def set_connection(conn: sqlite3.Connection | None) -> None:
    """Use a specific database (tests pass an in-memory one); None goes back to the file."""
    global _store
    _store = CacheStore(conn) if conn is not None else None


def _cache() -> CacheStore:
    global _store
    if _store is None:
        _store = CacheStore()
    return _store


def settings_for(service: str) -> dict | None:
    """The service's health settings, or None when it is not watched."""
    services = load_yaml("observability/pricing.yaml").get("services", {}) or {}
    return (services.get(service) or {}).get("health")


def _now() -> datetime:
    return datetime.now(UTC)


def _read(service: str) -> dict:
    return _cache().get(NAMESPACE, service) or {"failures": [], "down_until": None}


def _write(service: str, value: dict, keep_minutes: int) -> None:
    _cache().set(NAMESPACE, service, value, ttl=timedelta(minutes=keep_minutes))


def is_down(service: str, now: datetime | None = None) -> bool:
    cfg = settings_for(service)
    if not cfg:
        return False
    try:
        until = _read(service).get("down_until")
        return bool(until) and datetime.fromisoformat(until) > (now or _now())
    except (sqlite3.Error, ValueError) as err:
        logger.warning("Health monitor could not read %s: %s", service, err)
        return False


def record_failure(service: str, now: datetime | None = None) -> bool:
    """Count one failed call; returns True when this failure marks the source down."""
    cfg = settings_for(service)
    if not cfg:
        return False
    now = now or _now()
    try:
        state = _read(service)
        window = now - timedelta(minutes=cfg["window_minutes"])
        recent = [t for t in state.get("failures", []) if datetime.fromisoformat(t) > window] + [now.isoformat()]
        down = len(recent) >= cfg["failures"]
        state = {"failures": [] if down else recent,
                 "down_until": (now + timedelta(minutes=cfg["cooldown_minutes"])).isoformat() if down
                 else state.get("down_until")}
        _write(service, state, cfg["window_minutes"] + cfg["cooldown_minutes"])
        if down:
            logger.warning("Source %s marked down for %s minutes after %s failures; using fallbacks",
                           service, cfg["cooldown_minutes"], cfg["failures"])
        return down
    except (sqlite3.Error, ValueError) as err:
        logger.warning("Health monitor could not record %s: %s", service, err)
        return False


def record_success(service: str) -> None:
    if not settings_for(service):
        return
    try:
        _write(service, {"failures": [], "down_until": None}, 1)
    except sqlite3.Error as err:
        logger.warning("Health monitor could not record %s: %s", service, err)


def snapshot(now: datetime | None = None) -> dict[str, str]:
    """Every watched service as "up" or "down until HH:MM UTC", for the developer view."""
    services = load_yaml("observability/pricing.yaml").get("services", {}) or {}
    out = {}
    for name, cfg in services.items():
        if (cfg or {}).get("health"):
            until = _read(name).get("down_until") if is_down(name, now) else None
            out[name] = f"down until {datetime.fromisoformat(until):%H:%M} UTC" if until else "up"
    return out
