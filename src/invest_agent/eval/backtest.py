"""
eval/backtest.py: the monthly backtest run (design 14.17, Route A).

What this file does:
    Run it by hand once a month (it never runs inside a request):

        uv run python -m invest_agent.eval.backtest

    1. Verdicts. Every analysis in the decision log that is now 12 months old and not
       yet scored is judged: the stock's return over those 12 months against its
       benchmark's (eval/scoring.py), and whether the verdict held up. A stock that
       stopped trading during the year is scored to its last trading day and flagged.
    2. Forecasts. Every saved year-1 projection whose fiscal year has now been reported
       is compared with the actual revenue and net profit (eval/forecast_scoring.py).
       Each company's figures are fetched once per run, through the same fundamentals
       code the agent uses (cached for a day).
    3. The summary over everything scored so far: average excess return by verdict,
       hit rates by confidence, the calibration check (eval/calibration.py), forecast
       errors and range coverage, and how far there is to go before tuning: no weight or
       threshold suggestions before 100 scored verdicts.

    Results are kept in two tables (verdict_scores, forecast_scores), so each decision
    is scored once, and every run writes its report to data/backtests/ as JSON.

    It only ever suggests; it never changes the scorecard. A change is made by hand,
    after Aakash approves it, with a new config version (14.17).

    Live decisions take a year to mature, so for the first year this run reports what
    is waiting and when the first can be scored. The replay of past dates (Route B,
    eval/replay.py) is what gives results sooner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from invest_agent.agents.technical_price import yahoo_symbol
from invest_agent.common.records import utc_now_iso
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.eval import calibration, forecast_scoring, scoring
from invest_agent.storage.db import connect, transaction

logger = logging.getLogger(__name__)


# --- Data fetching (replaced by fakes in tests) ---------------------------------------------

async def fetch_prices(symbol: str, market: str) -> list[dict]:
    from invest_agent.mcp_servers.market_data.server import history_with_fallback
    return (await history_with_fallback(symbol, market))["rows"]


async def fetch_actuals(market: str, ticker: str, cik: str | None) -> dict[str, dict[int, float]]:
    """{"revenue": {fiscal_year: value}, "net_income": {...}} from the fundamentals code."""
    from invest_agent.mcp_servers.filings.server import fundamentals_logic
    result = await fundamentals_logic(market, ticker, cik)
    figures = result.get("figures") or {}
    return {f: {row["fiscal_year"]: row["value"] for row in figures.get(f, [])} for f in ("revenue", "net_income")}


def benchmark_for(market: str) -> str:
    return load_yaml("mcp_servers/market_data/benchmarks.yaml")[market]


# --- Reading the decision log -----------------------------------------------------------------

def _entries(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT d.request_id, d.created_at, d.ticker, d.verdict, d.confidence, d.payload_json FROM decision_log d "
        f"LEFT JOIN {table} s ON s.request_id = d.request_id WHERE s.request_id IS NULL ORDER BY d.created_at").fetchall()
    out = []
    for r in rows:
        e = dict(r)
        e["payload"] = json.loads(e.pop("payload_json"))
        out.append(e)
    return out


# --- 1. Verdicts ------------------------------------------------------------------------------

async def score_verdicts(conn: sqlite3.Connection, today: date, prices=fetch_prices) -> dict:
    months = scoring.config()["horizon_months"]
    cutoff = (today - timedelta(days=round(months * 365 / 12))).isoformat()
    waiting, scored, failed = [], [], []
    cache: dict[str, list[dict]] = {}

    async def rows_for(symbol: str, market: str) -> list[dict]:
        if symbol not in cache:
            try:
                cache[symbol] = await prices(symbol, market)
            except Exception as err:            # noqa: BLE001 - one company's failure is reported, not fatal
                logger.warning("Prices for %s failed: %s", symbol, err)
                cache[symbol] = []
        return cache[symbol]

    for e in _entries(conn, "verdict_scores"):
        if e["verdict"] not in scoring.SCORED_VERDICTS:
            continue
        if e["created_at"][:10] > cutoff:
            waiting.append({"ticker": e["ticker"], "due": scoring.window(e["created_at"], months)[1]})
            continue
        market = (e["payload"].get("company") or {}).get("market") or "US"
        stock = await rows_for(yahoo_symbol(e["ticker"], market), market)
        bench = await rows_for(benchmark_for(market), market)
        result, flags = scoring.excess_return(stock, bench, e["created_at"], months), []
        if result is None and stock and stock[-1]["date"] > e["created_at"][:10]:
            # Stopped trading during the year (delisted or taken over): score to its last day.
            s = scoring.period_return(stock, e["created_at"][:10], stock[-1]["date"])
            b = scoring.period_return(bench, e["created_at"][:10], stock[-1]["date"])
            if s and b:
                result = {"stock": s["return"], "benchmark": b["return"], "excess": s["return"] - b["return"],
                          "from": s["from"], "to": s["to"]}
                flags.append("ended_early")
        if result is None:
            failed.append({"ticker": e["ticker"], "request_id": e["request_id"], "reason": "no prices for the window"})
            continue
        correct = scoring.is_correct(e["verdict"], result["excess"])
        row = {**result, "verdict": e["verdict"], "confidence": e["confidence"], "correct": correct, "flags": flags}
        with transaction(conn):
            conn.execute("INSERT OR REPLACE INTO verdict_scores (request_id, scored_at, ticker, verdict, confidence, "
                         "excess_return, correct, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (e["request_id"], utc_now_iso(), e["ticker"], e["verdict"], e["confidence"],
                          result["excess"], int(bool(correct)), json.dumps(row)))
        scored.append({"ticker": e["ticker"], **row})
    return {"scored_now": scored, "waiting": waiting, "failed": failed}


# --- 2. Forecasts -----------------------------------------------------------------------------

async def score_forecasts(conn: sqlite3.Connection, today: date, actuals=fetch_actuals) -> dict:
    scored, waiting, failed = [], [], []
    cache: dict[str, dict] = {}
    for e in _entries(conn, "forecast_scores"):
        y1 = e["payload"].get("year1")
        if not y1 or not y1.get("fiscal_year"):
            continue
        fy = int(y1["fiscal_year"])
        if today < date(fy, 1, 1):                     # that fiscal year cannot have been reported yet
            waiting.append({"ticker": e["ticker"], "fiscal_year": fy})
            continue
        company = e["payload"].get("company") or {}
        key = f"{company.get('market')}:{e['ticker']}"
        if key not in cache:
            try:
                cache[key] = await actuals(company.get("market") or "US", e["ticker"], company.get("cik"))
            except Exception as err:            # noqa: BLE001
                logger.warning("Actual figures for %s failed: %s", e["ticker"], err)
                cache[key] = {}
        figs = cache[key]
        revenue, profit = (figs.get("revenue") or {}).get(fy), (figs.get("net_income") or {}).get(fy)
        if revenue is None and profit is None:
            waiting.append({"ticker": e["ticker"], "fiscal_year": fy})
            continue
        result = forecast_scoring.score_projection(y1, revenue, profit)
        rev, pro = result.get("revenue") or {}, result.get("profit") or {}
        with transaction(conn):
            conn.execute("INSERT OR REPLACE INTO forecast_scores (request_id, scored_at, ticker, fiscal_year, "
                         "revenue_error, revenue_inside, profit_error, profit_inside, result_json) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (e["request_id"], utc_now_iso(), e["ticker"], fy, rev.get("error"),
                          None if not rev else int(rev["inside"]), pro.get("error"),
                          None if not pro else int(pro["inside"]), json.dumps(result)))
        scored.append({"ticker": e["ticker"], **result})
    return {"scored_now": scored, "waiting": waiting, "failed": failed}


# --- 3. The summary ---------------------------------------------------------------------------

def summary(conn: sqlite3.Connection) -> dict:
    verdicts = [{**json.loads(r["result_json"]), "verdict": r["verdict"], "confidence": r["confidence"]}
                for r in conn.execute("SELECT verdict, confidence, result_json FROM verdict_scores").fetchall()]
    forecasts = [json.loads(r["result_json"]) for r in conn.execute("SELECT result_json FROM forecast_scores")]
    s = scoring.summarise(verdicts)
    need = scoring.config()["min_verdicts_for_tuning"]
    return {"verdicts": s, "calibration": calibration.check(s["by_confidence"]),
            "forecasts": forecast_scoring.summarise(forecasts),
            "tuning": ("ready: run eval/tuning.py" if s["count"] >= need
                       else f"not yet: {s['count']} of the {need} scored verdicts needed before any suggestion")}


async def run(conn: sqlite3.Connection | None = None, today: date | None = None,
              prices=fetch_prices, actuals=fetch_actuals) -> dict[str, Any]:
    conn = conn or connect()
    today = today or datetime.now(UTC).date()       # the decision log's times are UTC
    report = {"run_at": utc_now_iso(), "today": today.isoformat(),
              "verdicts_this_run": await score_verdicts(conn, today, prices),
              "forecasts_this_run": await score_forecasts(conn, today, actuals)}
    report["summary"] = summary(conn)
    return report


def save(report: dict, folder: str | None = None) -> Path:
    base = Path(folder or Path(get_settings().database_path).parent / "backtests")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"backtest_{report['today']}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def text(report: dict) -> list[str]:
    """The run in plain lines for the terminal."""
    v, f, s = report["verdicts_this_run"], report["forecasts_this_run"], report["summary"]
    lines = [f"Backtest, {report['today']}",
             f"Verdicts scored this run: {len(v['scored_now'])}; still maturing: {len(v['waiting'])}"
             + (f" (the first is due on {min(w['due'] for w in v['waiting'])})" if v["waiting"] else "")
             + (f"; could not score: {len(v['failed'])}" if v["failed"] else ""),
             f"Forecasts scored this run: {len(f['scored_now'])}; waiting for the annual report: {len(f['waiting'])}"]
    sv = s["verdicts"]
    lines.append(f"Scored so far: {sv['count']} verdict{'s' if sv['count'] != 1 else ''}" + (f", right {sv['hit_rate'] * 100:.0f}% of the time"
                                                               if sv["hit_rate"] is not None else ""))
    for name, row in sv["by_verdict"].items():
        if row["count"]:
            lines.append(f"  {name}: {row['count']}, average excess return {row['average_excess'] * 100:+.1f}%, "
                         f"right {row['hit_rate'] * 100:.0f}%")
    if sv["verdicts_in_order"] is not None:
        lines.append("  Favourable above neutral above unfavourable: " + ("yes" if sv["verdicts_in_order"] else "no"))
    lines.append(f"Confidence: {s['calibration']['finding']}")
    for field in ("revenue", "profit"):
        r = s["forecasts"][field]
        if r["count"]:
            lines.append(f"Forecast {field}: {r['count']} scored, base case off by {r['average_size_of_error'] * 100:.1f}% "
                         f"on average, {r['inside_range'] * 100:.0f}% inside the range"
                         + ("; the range looks too narrow" if r["range_too_narrow"] else ""))
    lines.append(f"Tuning: {s['tuning']}")
    return lines


def main() -> None:
    report = asyncio.run(run())
    path = save(report)
    print("\n".join(text(report)))
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
