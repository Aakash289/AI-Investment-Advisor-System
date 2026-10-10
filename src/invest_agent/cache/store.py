"""
cache/store.py: the one cache mechanism every cache in the app is built on.

What this file does:
    Keeps fetched data for a while so a repeat question does not spend API quota or
    time again. Each kind of data has its own lifetime (design 14.19): fundamentals
    24 hours, quotes 15 minutes, macro 24 hours, segments 7 days, FX 24 hours, peer
    medians 24 hours, news 60 minutes. The specific caches (price_cache.py and so on,
    built in step 14) are thin wrappers that pick a namespace and a lifetime and call
    this store.

    Values are stored as JSON text in the `cache` table of the SQLite database, so
    the cache survives restarts and is shared by the main app and the MCP servers.

Rules from the design:
    - An expired row counts as a miss.
    - A row that cannot be read (corrupt JSON) is deleted and counts as a miss, so
      the caller simply fetches fresh data (14.19 edge case).
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from invest_agent.storage.db import connect, transaction

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CacheStore:
    """Get and set JSON-serialisable values by (namespace, key), with an expiry."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        # Reuse a connection if the caller has one (tests pass an in-memory one);
        # otherwise open the app's database.
        self.conn = conn or connect()

    def get(self, namespace: str, key: str, now: datetime | None = None) -> Any | None:
        """Return the cached value, or None if it is missing, expired, or unreadable."""
        row = self.conn.execute(
            "SELECT value_json, expires_at FROM cache WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return None

        # ISO strings compare correctly by time once parsed back into datetimes.
        if datetime.fromisoformat(row["expires_at"]) <= (now or _now()):
            return None

        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            logger.warning("Dropping unreadable cache row %s/%s", namespace, key)
            self.delete(namespace, key)
            return None

    def set(self, namespace: str, key: str, value: Any, ttl: timedelta,
            now: datetime | None = None) -> None:
        """Store a value for the given lifetime, replacing any earlier value.

        json.dumps(default=str) turns values JSON cannot represent directly (dates,
        Decimal) into strings instead of failing.
        """
        created = now or _now()
        with transaction(self.conn):
            # "INSERT OR REPLACE" overwrites the row with the same primary key
            # (namespace, key) if one exists.
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (namespace, key, value_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    namespace,
                    key,
                    json.dumps(value, default=str),
                    created.isoformat(timespec="seconds"),
                    (created + ttl).isoformat(timespec="seconds"),
                ),
            )

    def delete(self, namespace: str, key: str) -> None:
        """Remove one entry, for example when a new 8-K should clear the news cache."""
        with transaction(self.conn):
            self.conn.execute("DELETE FROM cache WHERE namespace = ? AND key = ?", (namespace, key))

    def clear_namespace(self, namespace: str) -> int:
        """Remove every entry of one cache. Returns how many rows were removed."""
        with transaction(self.conn):
            return self.conn.execute("DELETE FROM cache WHERE namespace = ?", (namespace,)).rowcount
