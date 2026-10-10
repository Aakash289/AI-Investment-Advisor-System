"""
observability/quotas.py: how much of each free-plan limit has been used (design 14.16).

What this file does:
    tracked_call.py already adds every call's quota units to the quota_usage table, for
    today and for this month (each MCP server process writes to the same database file).
    This file reads those counts against the limits and says where things stand:

      status()     one row per limit: service, day or month, used, limit, share used,
                   and a level ("ok", "warn" from 80%, "out" at 100%)
      warnings()   plain lines for anything at 80% or more, shown after a run:
                   "Trendlyne: 1,640 of 2,000 calls used this month (82%)"
      remaining()  units left, used by callers that skip optional calls when a limit is
                   close (the Tavily credit guard already does this for news)

    The limits come from .env through config.py (TAVILY_MONTHLY_CREDIT_LIMIT and the
    others), so a plan change needs no code change. Counts are by UTC day and month,
    the same keys tracked_call writes.

    Run it on its own to see today's numbers:
        uv run python -m invest_agent.observability.quotas
"""

from __future__ import annotations

import sqlite3

from invest_agent.config import get_settings
from invest_agent.storage.db import connect, get_quota_units

NAMES = {"tavily": "Tavily", "trendlyne": "Trendlyne", "fmp": "Financial Modeling Prep"}
UNITS = {"tavily": "credits"}


def limits() -> list[tuple[str, str, int]]:
    """(service, "day" or "month", limit) for every free-plan limit the app tracks."""
    s = get_settings()
    return [("tavily", "month", s.tavily_monthly_credit_limit),
            ("trendlyne", "day", s.trendlyne_daily_call_limit),
            ("trendlyne", "month", s.trendlyne_monthly_call_limit),
            ("fmp", "day", s.fmp_daily_call_limit)]


def status(conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or connect()
    warn_at = get_settings().quota_warn_fraction
    rows = []
    for service, period, limit in limits():
        used = get_quota_units(conn, service, period)
        share = used / limit if limit else 0.0
        level = "out" if share >= 1 else "warn" if share >= warn_at else "ok"
        rows.append({"service": service, "period": period, "used": used, "limit": limit,
                     "share": round(share, 4), "level": level})
    return rows


def remaining(service: str, period: str, conn: sqlite3.Connection | None = None) -> int | None:
    """Units left in the period, or None when the service has no such limit."""
    for row in status(conn):
        if row["service"] == service and row["period"] == period:
            return max(0, row["limit"] - row["used"])
    return None


def warnings(conn: sqlite3.Connection | None = None) -> list[str]:
    """A plain line for every limit at 80% or more."""
    out = []
    for r in status(conn):
        if r["level"] == "ok":
            continue
        when = "today" if r["period"] == "day" else "this month"
        unit = UNITS.get(r["service"], "calls")
        line = (f"{NAMES.get(r['service'], r['service'])}: {r['used']:,} of {r['limit']:,} {unit} used {when} "
                f"({r['share'] * 100:.0f}%)")
        if r["level"] == "out":
            line += "; the limit is reached, so calls to it will fail until it resets"
        out.append(line)
    return out


def main() -> None:
    print(f"{'Service':<26}{'Period':<8}{'Used':>8}{'Limit':>8}{'Share':>8}")
    for r in status():
        print(f"{NAMES.get(r['service'], r['service']):<26}{r['period']:<8}{r['used']:>8,}{r['limit']:>8,}"
              f"{r['share'] * 100:>7.0f}%")
    for w in warnings():
        print("Warning:", w)


if __name__ == "__main__":
    main()
