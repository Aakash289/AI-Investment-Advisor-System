"""
observability/decision_log.py: one saved entry per analysis (design 14.19).

What this file does:
    After every request that reached a verdict, main.py calls log_decision(). It writes
    one row to the decision_log table: a few columns worth sorting and filtering on
    (time, ticker, verdict, confidence, amount, config and disclaimer versions, cost,
    time taken, report path), and everything else as one JSON document:

      profile            the person's profile as it was then (it may change later)
      records            every verified source record the analysis stood on
      verification       sanity flags, conflicts, completeness, data notes
      scores             the sub-scores, findings and the scorecard
      model              the numbered facts Synthesis saw and what it wrote
      guardrail          every number checked, every sentence removed, the flags
      sizing             every sizing step
      year1              next year's projected revenue and EPS (low, base, high), the
                         fair value range and today's price: what the backtesting
                         harness (step 15) scores when the next annual report lands
      run                cost, time taken, step timings, outside calls, quota warnings

    So it is always possible to see exactly what the person was shown and why, and to
    check the projections later against what really happened.

    Questions that end a turn (which company, or what budget) are not logged: they are
    not analyses. Entries older than 2 years are removed by the monthly clean-up, which
    maybe_cleanup() runs at most once every 30 days.

    Run it on its own to list the latest entries:
        uv run python -m invest_agent.observability.decision_log
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from typing import Any

from invest_agent.cache.store import CacheStore
from invest_agent.common.records import utc_now_iso
from invest_agent.storage.db import cleanup_old_entries, connect, transaction

logger = logging.getLogger(__name__)

LOGGED_VERDICTS = {"favourable", "neutral", "unfavourable", "insufficient_data", "not_covered"}


def year1(state: dict) -> dict | None:
    """Next year's projections and the fair value range, for scoring later (14.10, 14.17)."""
    proj = state.get("projection") or {}
    if proj.get("status") != "ok" or not proj.get("years"):
        return None
    y1 = proj["years"][0]
    cases = {case: {"revenue": rows[y1].get("revenue"), "net_income": rows[y1].get("net_income"),
                    "eps": rows[y1].get("eps")}
             for case, rows in (proj.get("cases") or {}).items() if y1 in rows}
    fv = state.get("fair_value") or {}
    price = next((r["value"] for r in state.get("verified_records") or [] if r.get("field") == "current_price"), None)
    return {"fiscal_year": y1, "base_year": proj.get("base_year"), "cases": cases,
            "fair_value": fv.get("range") if fv.get("status") == "ok" else None,
            "fair_value_reason": fv.get("reason") if fv.get("status") != "ok" else None,
            "price_at_analysis": price, "currency": state.get("listing_currency"),
            "growth": (proj.get("assumptions") or {}).get("growth"), "flags": proj.get("flags", [])}


def build_entry(state: dict, *, run: dict) -> tuple[dict, dict]:
    """(the row's columns, the JSON payload) for one analysis."""
    card = state.get("scorecard") or {}
    sizing = state.get("sizing") or {}
    columns = {
        "request_id": state.get("request_id") or run.get("request_id"),
        "created_at": utc_now_iso(),
        "ticker": state.get("ticker"), "exchange": state.get("exchange"),
        "listing_currency": state.get("listing_currency"),
        "verdict": state.get("verdict") or state.get("preliminary_verdict"),
        "confidence": state.get("confidence") or state.get("preliminary_confidence"),
        "recommended_amount": sizing.get("amount"), "amount_currency": sizing.get("currency"),
        "config_version": str(card.get("config_version")) if card.get("config_version") is not None else None,
        "disclaimer_version": str(state.get("disclaimer_version") or "") or None,
        "cost_usd": (run.get("cost") or {}).get("total_usd"),
        "latency_s": run.get("latency_s"),
        "report_path": state.get("report_path"),
    }
    payload = {
        "question": state.get("raw_input"),
        "company": {"name": state.get("company_name"), "market": state.get("market"), "cik": state.get("cik"),
                    "sic_code": state.get("sic_code"), "sector": state.get("sector"),
                    "industry": state.get("industry"), "bank_mode": state.get("bank_mode")},
        "profile": state.get("user_profile"),
        "records": state.get("verified_records"),
        "projection_records": state.get("projection_records"),
        "verification": {"sanity_flags": state.get("sanity_flags"), "conflicts": state.get("conflicts"),
                         "completeness": state.get("completeness_detail"), "data_notes": state.get("data_notes"),
                         "amount_withheld": state.get("amount_withheld")},
        "scores": {"sub_scores": state.get("sub_scores"), "findings": state.get("findings"), "scorecard": card,
                   "suitability_notes": state.get("suitability_notes"), "detail": state.get("analysis_detail")},
        "model": {"facts": state.get("facts"), "synthesis": state.get("synthesis"),
                  "verdict_change_reason": state.get("verdict_change_reason")},
        "guardrail": {"log": state.get("guardrail_log"), "removed": state.get("removed_sentences"),
                      "flags": state.get("guardrail_flags"), "phrase_removals": state.get("phrase_removals")},
        "sizing": sizing,
        "year1": year1(state),
        "answer": state.get("final_response"),
        "run": run,
    }
    return columns, payload


def log_decision(state: dict, *, run: dict, conn: sqlite3.Connection | None = None) -> str | None:
    """Write the entry; returns its request id, or None when nothing was logged. A
    database problem is logged and never fails the request."""
    columns, payload = build_entry(state, run=run)
    if columns["verdict"] not in LOGGED_VERDICTS or not columns["request_id"]:
        return None
    try:
        conn = conn or connect()
        names = list(columns) + ["payload_json"]
        values = list(columns.values()) + [json.dumps(payload, default=str)]
        with transaction(conn):
            conn.execute(f"INSERT OR REPLACE INTO decision_log ({', '.join(names)}) "
                         f"VALUES ({', '.join('?' for _ in names)})", values)
        return columns["request_id"]
    except sqlite3.Error as err:
        logger.warning("Could not write the decision log: %s", err)
        return None


def recent(n: int = 10, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or connect()
    rows = conn.execute("SELECT request_id, created_at, ticker, verdict, confidence, recommended_amount, "
                        "amount_currency, cost_usd, latency_s FROM decision_log ORDER BY created_at DESC LIMIT ?",
                        (n,)).fetchall()
    return [dict(r) for r in rows]


def get(request_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """One entry with its payload, for looking back at an analysis."""
    conn = conn or connect()
    row = conn.execute("SELECT * FROM decision_log WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json"))
    return out


def maybe_cleanup(conn: sqlite3.Connection | None = None, every_days: int = 30) -> dict[str, Any] | None:
    """Run the 2-year clean-up (storage/db.py) at most once every `every_days` days."""
    try:
        conn = conn or connect()
        cache = CacheStore(conn)
        if cache.get("maintenance", "cleanup") is not None:
            return None
        result = cleanup_old_entries(conn)
        cache.set("maintenance", "cleanup", {"ran_at": utc_now_iso(), **result}, ttl=timedelta(days=every_days))
        return result
    except sqlite3.Error as err:
        logger.warning("Clean-up did not run: %s", err)
        return None


def main() -> None:
    rows = recent(15)
    if not rows:
        print("No analyses logged yet.")
        return
    print(f"{'When (UTC)':<20}{'Ticker':<12}{'Verdict':<14}{'Conf.':<8}{'Amount':>14}{'Cost $':>9}{'Secs':>7}")
    for r in rows:
        amount = f"{r['recommended_amount']:,.0f} {r['amount_currency'] or ''}" if r["recommended_amount"] else "-"
        cost = f"{r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
        secs = f"{r['latency_s']:.0f}" if r["latency_s"] is not None else "-"
        print(f"{r['created_at'][:19]:<20}{r['ticker'] or '-':<12}{r['verdict'] or '-':<14}{r['confidence'] or '-':<8}"
              f"{amount:>14}{cost:>9}{secs:>7}")


if __name__ == "__main__":
    main()
