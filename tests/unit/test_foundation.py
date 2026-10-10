"""
Unit tests for build step 1, the foundation: YAML loader, source records, SQLite
storage, the cache store, and tracked_call.

Every test uses an in-memory SQLite database and no network, so the whole file runs
in well under a second. Run with:
    uv run pytest tests/unit/test_foundation.py -v
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from invest_agent.cache.store import CacheStore
from invest_agent.common.records import make_record
from invest_agent.common.yaml_loader import ConfigFileError, load_yaml
from invest_agent.observability import tracked_call as tc
from invest_agent.storage.db import add_quota_units, cleanup_old_entries, connect, get_quota_units


# A pytest "fixture" is a reusable setup step: any test that names `conn` as an
# argument receives a fresh in-memory database, thrown away after the test.
@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


# --- YAML loader and records -------------------------------------------------------

def test_yaml_loader_reads_package_file():
    data = load_yaml("mcp_servers/entity/entity_config.yaml")
    assert data["match"]["min_score"] == 90


def test_yaml_loader_missing_file_is_clear_error():
    with pytest.raises(ConfigFileError):
        load_yaml("does/not/exist.yaml")


def test_record_id_is_stable_and_optional_keys_kept():
    r = make_record(source="sec_edgar", tier=1, entity="AAPL", field="revenue", value=391e9,
                    period="FY2024", unit="USD", fiscal_year=2024, accession_number=None)
    assert r["record_id"] == "sec_edgar:AAPL:revenue:FY2024"
    assert r["unit"] == "USD" and "accession_number" not in r   # None values are dropped


def test_record_refuses_missing_value():
    # Design rule: missing stays missing, never an empty record.
    with pytest.raises(ValueError):
        make_record(source="sec_edgar", tier=1, entity="AAPL", field="debt", value=None)


# --- Storage -----------------------------------------------------------------------

def test_quota_counts_by_day_and_month(conn):
    day1 = datetime(2026, 10, 4, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 5, tzinfo=timezone.utc)
    add_quota_units(conn, "tavily", 2, now=day1)
    add_quota_units(conn, "tavily", 3, now=day2)
    assert get_quota_units(conn, "tavily", "day", now=day2) == 3     # a new day starts fresh
    assert get_quota_units(conn, "tavily", "month", now=day2) == 5   # the month keeps adding


def test_cleanup_removes_entries_older_than_retention(conn, tmp_path):
    # tmp_path is a built-in pytest fixture: a temporary folder for this test.
    old_pdf = tmp_path / "old.pdf"
    old_pdf.write_bytes(b"%PDF")
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    rows = [
        ("old", (now - timedelta(days=800)).isoformat(), str(old_pdf)),
        ("new", (now - timedelta(days=10)).isoformat(), None),
    ]
    for rid, created, path in rows:
        conn.execute(
            "INSERT INTO decision_log (request_id, created_at, report_path, payload_json) VALUES (?, ?, ?, '{}')",
            (rid, created, path),
        )
    conn.commit()

    result = cleanup_old_entries(conn, retention_days=730, now=now)

    remaining = [r["request_id"] for r in conn.execute("SELECT request_id FROM decision_log")]
    assert remaining == ["new"]
    assert result["report_files"] == 1 and not old_pdf.exists()


# --- Cache store ---------------------------------------------------------------------

def test_cache_hit_then_expiry(conn):
    cache = CacheStore(conn)
    t0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    cache.set("price", "AAPL:quote", {"price": 250.1}, ttl=timedelta(minutes=15), now=t0)
    assert cache.get("price", "AAPL:quote", now=t0 + timedelta(minutes=5)) == {"price": 250.1}
    assert cache.get("price", "AAPL:quote", now=t0 + timedelta(minutes=16)) is None


def test_corrupt_cache_row_is_dropped(conn):
    conn.execute(
        "INSERT INTO cache VALUES ('news', 'AAPL', '{not json', '2026-10-04T00:00:00+00:00', '2999-01-01T00:00:00+00:00')"
    )
    conn.commit()
    cache = CacheStore(conn)
    assert cache.get("news", "AAPL") is None
    assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0


# --- tracked_call ----------------------------------------------------------------------

def test_tracked_call_retries_then_succeeds_and_counts_quota(conn):
    tc.set_quota_connection(conn)
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("temporary")
        return "ok"

    async def run():
        log = tc.start_call_log()
        result = await tc.tracked_call("tavily", "search", flaky, quota_units=1, retries=2)
        return result, log

    result, log = asyncio.run(run())
    assert result == "ok"
    assert log[0]["attempts"] == 2 and log[0]["ok"] is True
    assert get_quota_units(conn, "tavily", "month") == 1
    tc.set_quota_connection(None)


def test_tracked_call_does_not_retry_a_404(conn):
    tc.set_quota_connection(conn)
    calls = {"n": 0}

    async def not_found():
        calls["n"] += 1
        request = httpx.Request("GET", "https://example.org")
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    with pytest.raises(tc.ToolCallFailed):
        asyncio.run(tc.tracked_call("fmp", "search", not_found, retries=2))
    assert calls["n"] == 1                                  # a bad request is not repeated
    assert get_quota_units(conn, "fmp", "day") == 0         # failures do not use quota
    tc.set_quota_connection(None)


def test_tracked_call_runs_plain_functions_with_timeout(conn):
    tc.set_quota_connection(conn)

    def slow_sync():
        import time
        time.sleep(0.5)

    with pytest.raises(tc.ToolCallFailed):
        asyncio.run(tc.tracked_call("yfinance", "history", slow_sync, timeout=0.05, retries=0))
    assert asyncio.run(tc.tracked_call("yfinance", "history", lambda: 42)) == 42
    tc.set_quota_connection(None)


# --- Secrets never reach the logs (added 5 October) -------------------------------------

def test_http_request_lines_are_hidden_and_tokens_redacted(caplog):
    import logging
    import invest_agent                                     # the rule is set when the package loads
    http = logging.getLogger("httpx")
    assert http.getEffectiveLevel() >= logging.WARNING      # request lines are not logged at all
    # Even if someone lowers the level while debugging, the token is blanked out.
    http.setLevel(logging.INFO)
    try:
        with caplog.at_level(logging.INFO):
            http.info('HTTP Request: POST %s "200 OK"', "https://mcp.trendlyne.com/mcp/?token=abc123secret&x=1")
            http.info("GET https://api.polygon.io/v2/aggs?apiKey=KEY999")
            logging.getLogger("invest_agent.anything").warning(
                "Trendlyne failed: Client error '401' for url 'https://mcp.trendlyne.com/mcp/?token=OTHERSECRET'")
        text = caplog.text
        assert "abc123secret" not in text and "KEY999" not in text and "OTHERSECRET" not in text
        assert "token=***" in text
    finally:
        http.setLevel(logging.WARNING)