"""
storage/db.py: opens the app's SQLite database and keeps it tidy.

What this file does:
    1. connect() opens the database file (creating it and its folder if needed),
       applies schema.sql, and returns a ready connection.
    2. transaction() is a small helper for "do these writes together or not at all".
    3. add_quota_units() / get_quota_units() keep the free-plan usage counts that
       tracked_call writes on every outside call.
    4. cleanup_old_entries() deletes decision log rows older than the retention
       period (2 years by default), their saved PDF files, and expired cache rows.

Why SQLite (14.19): it is built into Python, stores everything in one file, needs no
server, and is enough for one user. Each process (the main app and each MCP server)
opens its own connection; SQLite's file lock keeps concurrent writers safe.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from invest_agent.config import get_settings

logger = logging.getLogger(__name__)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database, create any missing tables, and return the connection.

    path: defaults to DATABASE_PATH from .env. Pass ":memory:" in tests for a
    throwaway database that lives only in memory.
    """
    db_path = str(path or get_settings().database_path)
    if db_path != ":memory:":
        # Make sure the folder exists (data/ is not committed to git).
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # sqlite3 is Python's built-in SQLite driver.
    #   timeout=10                wait up to 10 s if another process holds the write lock
    #   check_same_thread=False   allow use from asyncio worker threads; we never share
    #                             one connection between threads at the same time
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)

    # Row factory: rows come back as sqlite3.Row, which can be read by column name
    # (row["ticker"]) as well as by position.
    conn.row_factory = sqlite3.Row

    # PRAGMAs are SQLite settings for this connection.
    #   journal_mode=WAL   readers do not block the writer, useful when an MCP server
    #                      reads the cache while the main app writes the decision log
    #   foreign_keys=ON    enforce foreign keys if any are added later
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Run schema.sql, which ships inside the package next to this file.
    schema = resources.files("invest_agent.storage").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Group several writes so they are saved together or not at all.

    Usage:
        with transaction(conn):
            conn.execute(...)
            conn.execute(...)
    If anything inside the block raises, every write in it is rolled back.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --- Quota counts ------------------------------------------------------------------

def _period_keys(now: datetime) -> dict[str, str]:
    """The day and month keys a usage count is filed under, in UTC."""
    return {"day": now.strftime("%Y-%m-%d"), "month": now.strftime("%Y-%m")}


def add_quota_units(conn: sqlite3.Connection, service: str, units: int = 1,
                    now: datetime | None = None) -> None:
    """Add units of usage for a service to today's and this month's counts.

    "INSERT ... ON CONFLICT DO UPDATE" (an upsert) creates the row the first time
    and adds to it after that, in one statement.
    """
    if units <= 0:
        return
    keys = _period_keys(now or datetime.now(timezone.utc))
    with transaction(conn):
        for kind, key in keys.items():
            conn.execute(
                """
                INSERT INTO quota_usage (service, period_kind, period_key, units)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (service, period_kind, period_key)
                DO UPDATE SET units = units + excluded.units
                """,
                (service, kind, key, units),
            )


def get_quota_units(conn: sqlite3.Connection, service: str, period_kind: str,
                    now: datetime | None = None) -> int:
    """Units used so far today ("day") or this month ("month") for a service."""
    key = _period_keys(now or datetime.now(timezone.utc))[period_kind]
    row = conn.execute(
        "SELECT units FROM quota_usage WHERE service = ? AND period_kind = ? AND period_key = ?",
        (service, period_kind, key),
    ).fetchone()
    return int(row["units"]) if row else 0


# --- Retention clean-up --------------------------------------------------------------

def cleanup_old_entries(conn: sqlite3.Connection, retention_days: int | None = None,
                        now: datetime | None = None) -> dict[str, int]:
    """Delete decision log entries older than the retention period, plus their saved
    PDF reports, and every expired cache row. Meant to run once a month (14.19).

    Returns how many rows and files were removed, for the log.
    """
    days = retention_days if retention_days is not None else get_settings().decision_log_retention_days
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")

    old = conn.execute(
        "SELECT request_id, report_path FROM decision_log WHERE created_at < ?", (cutoff,)
    ).fetchall()

    # Remove the PDF files first; a missing file is not an error (it may have been
    # deleted by hand). Path.unlink(missing_ok=True) ignores a file that is not there.
    files_removed = 0
    for row in old:
        if row["report_path"]:
            report = Path(row["report_path"])
            if report.exists():
                report.unlink(missing_ok=True)
                files_removed += 1

    with transaction(conn):
        log_removed = conn.execute("DELETE FROM decision_log WHERE created_at < ?", (cutoff,)).rowcount
        cache_removed = conn.execute(
            "DELETE FROM cache WHERE expires_at < ?", (now.isoformat(timespec="seconds"),)
        ).rowcount

    result = {"decision_log_rows": log_removed, "report_files": files_removed, "cache_rows": cache_removed}
    logger.info("Clean-up removed %s", result)
    return result
