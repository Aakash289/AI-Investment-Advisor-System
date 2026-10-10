"""
scripts/cache_peek.py: shows what the shared cache holds for news, and until when.

What this file does:
    Prints, for the news results and the saved news labels, how many entries there
    are and when each news entry was saved and expires (times in UTC). It prints keys
    and times only, never the cached content, so it shows no web addresses.

Run: uv run python scripts/cache_peek.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from invest_agent.cache.store import CacheStore

conn = CacheStore().conn
print("Now (UTC):", datetime.now(timezone.utc).isoformat(timespec="seconds"))

# News results, one per company: saved and expiry times.
for row in conn.execute("SELECT key, created_at, expires_at FROM cache WHERE namespace = 'news' ORDER BY key"):
    print(f"news  {row['key']:<16} saved {row['created_at']}  expires {row['expires_at']}")

# Saved labels: a count per company (the keys themselves are hashes).
for row in conn.execute("SELECT substr(key, 1, instr(key, ':') - 1) AS company, COUNT(*) AS n, "
                        "MAX(created_at) AS latest FROM cache WHERE namespace = 'news_labels' GROUP BY company"):
    print(f"labels {row['company']:<15} {row['n']} saved, latest {row['latest']}")
