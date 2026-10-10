"""
profile/store.py: remembers the user's profile between sessions, in SQLite.

What this file does:
    A profile is asked for once and reused (design 6.2 and 14.2). After each message,
    the merged profile is saved to the `profiles` table in the app's SQLite file; the
    next request loads it, so "should I invest in Infosys" works without repeating
    the budget and risk details.

    Only what the user actually said is saved: values filled in by defaults, and the
    "assumed" markers, are never stored, so an assumption made once does not quietly
    become the user's stated profile.

    The app is single-user for now, so the user ID is "local".
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from invest_agent.common.records import utc_now_iso
from invest_agent.profile.followup import STORED_FIELDS
from invest_agent.storage.db import connect, transaction

LOCAL_USER = "local"


class ProfileStore:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn or connect()

    def load(self, user_id: str = LOCAL_USER) -> dict[str, Any]:
        """The stored profile, or an empty dict for a first-time user."""
        row = self.conn.execute("SELECT profile_json FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(row["profile_json"])
        except json.JSONDecodeError:
            return {}          # an unreadable row is treated as no profile; it is overwritten on save

    def save(self, profile: dict[str, Any], user_id: str = LOCAL_USER) -> None:
        """Save the user's stated fields only."""
        stated = {k: v for k, v in profile.items() if k in STORED_FIELDS and v is not None}
        with transaction(self.conn):
            # "INSERT ... ON CONFLICT DO UPDATE" creates the row the first time and
            # replaces it after that.
            self.conn.execute(
                "INSERT INTO profiles (user_id, profile_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET profile_json = excluded.profile_json, "
                "updated_at = excluded.updated_at",
                (user_id, json.dumps(stated), utc_now_iso()),
            )

    def updated_at(self, user_id: str = LOCAL_USER) -> str | None:
        """When the profile was last saved (the app's profile tab shows it)."""
        row = self.conn.execute("SELECT updated_at FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return row["updated_at"] if row else None

    def clear(self, user_id: str = LOCAL_USER) -> bool:
        """Forget the saved profile (the app's "forget my profile" button). True if one existed."""
        with transaction(self.conn):
            cur = self.conn.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0
