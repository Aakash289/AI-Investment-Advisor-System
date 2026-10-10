"""
eval/replay.py: Route B, replaying past dates for US companies (design 14.17, build step 15b).

What this file does:
    On each past date it takes the largest companies by market value on that date from the
    candidates in eval/replay_universe.yaml (version 5: not today's largest, which would
    pick winners with hindsight). For each of them it rebuilds what
    the agent would have seen on that date and runs the same scorecard the live agent
    runs, then scores the verdict on the 12 months that followed:

      figures       SEC company facts, keeping only what had been filed by that date
                    (normaliser.normalise_company_facts with as_of)
      prices        Yahoo's daily history cut off at that date, for the price figures,
                    volatility and beta; the company's price on the day is put back to
                    what it really was (Yahoo divides old prices by any later split, while
                    the share count from that year's 10-K is the real one)
      peers         today's FMP peer list (survivorship: see below) plus every
                    same-sector company from the replay list that traded on the date
                    (version 3); each peer's market value moved back to the date by its
                    price change; the SEC frames for the calendar year fully reported by
                    that date; then the live size floor and 5-peer minimum as usual
      the rest      the live verification step (verify) and analysis step (analyse),
                    unchanged, with a standard profile (medium risk, 10 years, growth)

    Left out, by design: news (GDELT keeps only three months and Tavily searches today's
    web), the economic fit (the IMF and BIS series are today's, not as they were then),
    and geography. Their weights are shared among the other sub-scores, as the live
    scorecard does for any missing one. Completeness is therefore lower than in a live
    run, so it is not allowed to lower confidence here.

    Every replayed verdict is scored with eval/scoring.py: the 12-month return against the
    S&P 500, right or wrong. Results go into the replay_scores table, so a long run can be
    stopped and started again without repeating work.

    Cautions printed with every report:
      survivorship   reduced but not removed: companies taken over or delisted before
                     today cannot be priced, so they are missing
      peers          today's peer list is used for every date
      frames         SEC frames hold the latest filed value for each year, so a later
                     restatement of a peer's figures can show through

    Run it (about 30 to 60 minutes the first time, mostly Yahoo and SEC downloads):
        uv run python -m invest_agent.eval.replay
        uv run python -m invest_agent.eval.replay --limit 5      (a quick trial)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import traceback
from datetime import UTC, date, datetime, timedelta
from typing import Any

from invest_agent.agents.fundamentals import build_records
from invest_agent.agents.technical_price import SKIP, UNITS
from invest_agent.analysis.step import analyse
from invest_agent.cache.store import CacheStore
from invest_agent.common.records import make_record, utc_now_iso
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.eval import calibration, scoring, tuning
from invest_agent.mcp_servers.filings.metrics import compute_metrics
from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts
from invest_agent.mcp_servers.market_data.metrics import compute_price_metrics
from invest_agent.observability.tracked_call import tracked_call
from invest_agent.storage.db import connect, transaction
from invest_agent.verification.step import verify

logger = logging.getLogger(__name__)

CAUTIONS = [
    ("Survivorship, reduced: on each date the largest companies then are chosen from about 100 candidates, "
     "including many that later fell; companies taken over or delisted before today cannot be priced and "
     "are missing."),
    "Peers: today's peer list is used for every past date.",
    "Left out: news, economic fit and geography (their weights are shared out); the live agent uses all of them.",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_scores (
    universe_version INTEGER NOT NULL,
    ticker           TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    status           TEXT NOT NULL,
    verdict          TEXT,
    confidence       TEXT,
    score            REAL,
    excess_return    REAL,
    correct          INTEGER,
    result_json      TEXT NOT NULL,
    scored_at        TEXT NOT NULL,
    PRIMARY KEY (universe_version, ticker, as_of)
);
"""


def universe() -> dict:
    """The replay list, with a flat "companies" list and "sector_of" built from the sectors."""
    u = dict(load_yaml("eval/replay_universe.yaml"))
    sectors = u.get("sectors") or {}
    if sectors and "companies" not in u:
        u["companies"] = [t for tickers in sectors.values() for t in tickers]
    u["sector_of"] = {t: name for name, tickers in sectors.items() for t in tickers}
    return u




# --- Downloads (each replaced by a fake in the tests) -----------------------------------------

def _yahoo_with_splits(symbol: str) -> list[dict]:
    """Daily rows with the split ratio of the day: {date, close, adj_close, split}."""
    import yfinance as yf
    table = yf.Ticker(symbol).history(period="10y", interval="1d", auto_adjust=False)
    rows = []
    for when, row in table.iterrows():
        close = row.get("Close")
        if close is None or math.isnan(close):
            continue
        adj = row.get("Adj Close")
        split = row.get("Stock Splits") or 0.0
        rows.append({"date": when.date().isoformat(), "close": float(close),
                     "adj_close": float(adj) if adj is not None and not math.isnan(adj) else float(close),
                     "volume": float(row.get("Volume") or 0),
                     "split": 0.0 if math.isnan(float(split)) else float(split)})
    return rows


YAHOO_WAITS = [5, 15, 30]                      # seconds before the 2nd, 3rd and 4th try


async def _yahoo(operation: str, fn, symbol: str):
    """A Yahoo call for the replay: its own service name ("yfinance_replay", paced at one
    call every 1.5 seconds and not watched by the Source Health Monitor), and up to four
    tries with growing waits, since Yahoo answers a burst of requests with rate-limit
    errors (found in the 7 October trial, where the monitor then marked Yahoo down and
    every later peer was dropped without a word)."""
    for attempt, wait in enumerate([0, *YAHOO_WAITS]):
        if wait:
            await asyncio.sleep(wait)
        try:
            return await tracked_call("yfinance_replay", operation, fn, symbol, timeout=60, retries=0)
        except Exception as err:                # noqa: BLE001 - retried, then raised
            last = err
            logger.warning("Yahoo %s for %s failed (try %s): %s", operation, symbol, attempt + 1, err)
    raise last


class Sources:
    """Where the replay gets its data. Tests pass fakes for every method. Price histories
    and market values are kept in memory for the run, since the same large companies are
    peers of many others."""

    def __init__(self) -> None:
        self._prices: dict[str, list[dict]] = {}
        self._caps: dict[str, float | None] = {}

    async def company_facts(self, cik: str) -> dict:
        from invest_agent.mcp_servers.filings import sec_edgar
        return await tracked_call("sec_edgar", "company_facts", sec_edgar.fetch_company_facts, cik, timeout=90)

    async def prices(self, symbol: str) -> list[dict]:
        if symbol not in self._prices:
            self._prices[symbol] = await _yahoo("history_10y", _yahoo_with_splits, symbol)
        return self._prices[symbol]

    async def peer_list(self, ticker: str) -> list[dict]:
        cache = CacheStore()
        if (hit := cache.get("replay_peers", ticker)) is not None:
            return hit
        from invest_agent.mcp_servers.filings.peers_us import fmp_peers
        peers = await fmp_peers(ticker)
        cache.set("replay_peers", ticker, peers, ttl=timedelta(days=30))
        return peers

    async def market_cap(self, symbol: str) -> float | None:
        """Today's market value from Yahoo, for a sector peer FMP did not list (cached 30 days)."""
        if symbol in self._caps:
            return self._caps[symbol]
        cache = CacheStore()
        if (hit := cache.get("replay_mcap", symbol)) is not None and hit.get("value"):
            self._caps[symbol] = hit["value"]
            return hit["value"]

        def read(sym: str) -> float | None:
            import yfinance as yf
            value = yf.Ticker(sym).fast_info["market_cap"]
            return float(value) if value else None
        value = await _yahoo("market_cap", read, symbol)
        if value:                                   # only a real value is kept for 30 days
            cache.set("replay_mcap", symbol, {"value": value}, ttl=timedelta(days=30))
        self._caps[symbol] = value
        return value

    async def frame(self, tag: str, unit: str, period: str, taxonomy: str = "us-gaap") -> dict:
        from invest_agent.mcp_servers.filings.peers_us import sec_frame
        return await sec_frame(tag, unit, period, taxonomy)

    async def ciks(self) -> dict[str, str]:
        from invest_agent.mcp_servers.entity.reference_data import load_reference_data
        ref = await load_reference_data()
        return {listing.ticker: str(listing.cik) for listing in ref.listings if listing.cik}


# --- Prices on a past date --------------------------------------------------------------------

def until(rows: list[dict], as_of: str) -> list[dict]:
    """The rows up to and including the date, with the close put back to the real price
    of the day: Yahoo divides old closes by every later split, so multiply them back."""
    later = 1.0
    for r in rows:
        if r["date"] > as_of and r.get("split"):
            later *= r["split"]
    return [{**r, "close": r["close"] * later} for r in rows if r["date"] <= as_of]


def price_records(rows: list[dict], bench: list[dict], ticker: str, as_of: str) -> list[dict]:
    result = compute_price_metrics(until(rows, as_of), until(bench, as_of), "USD")
    out = []
    for field, value in (result.get("metrics") or {}).items():
        if field in SKIP or value is None:
            continue
        unit = "USD" if UNITS.get(field) == "currency" else UNITS.get(field, "number")
        out.append(make_record(source="yfinance", tier=2, entity=ticker, field=field, value=value,
                               as_of=as_of, period=as_of, unit=unit))
    return out


def adj_close_on(rows: list[dict], day: str) -> float | None:
    before = [r for r in rows if r["date"] <= day]
    return before[-1]["adj_close"] if before else None


# --- One company on one date ------------------------------------------------------------------

async def replay_one(ticker: str, cik: str, as_of: str, *, facts: dict, rows: list[dict], bench: list[dict],
                     peers_now: list[dict], peer_rows: dict[str, list[dict]], src: Sources,
                     ciks: dict[str, str], profile: dict) -> dict:
    """The scorecard as it would have come out on `as_of`, and how the next 12 months went."""
    from invest_agent.mcp_servers.filings.peers_us import us_peer_multiples

    figures = normalise_company_facts(facts, as_of=as_of)
    if not figures.get("revenue") or not figures.get("net_income"):
        # Nothing to score (Exxon on 7 October): say what the SEC file did hold, so the
        # tag map can be extended, instead of failing further down.
        gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        return {"status": "no_figures", "figures_found": sorted(figures),
                "revenue_like_tags": sorted(t for t in gaap if "Revenue" in t)[:15]}
    fund = {"source": "sec_edgar", "tier": 1, "figures": figures,
            "metrics": compute_metrics(figures, bank_mode=False), "currency": "USD"}
    state: dict[str, Any] = {
        "ticker": ticker, "market": "US", "listing_currency": "USD", "company_name": ticker, "cik": cik,
        "fundamentals_records": build_records(fund, ticker), "price_records": price_records(rows, bench, ticker, as_of),
        "user_profile": profile, "bank_mode": False,
    }

    async def peers_then(market: str, tick: str, industry: str | None) -> dict:
        scaled = []
        for p in peers_now:
            series = peer_rows.get(p["symbol"]) or []
            then, now = adj_close_on(series, as_of), (series[-1]["adj_close"] if series else None)
            if then and now and p.get("market_value"):
                scaled.append({**p, "market_value": p["market_value"] * then / now})   # not listed then: left out
        # Today's FMP peers and every same-sector company from the replay list that traded
        # on the date; the live size floor and the 5-peer minimum then decide as usual.
        return await us_peer_multiples(tick, today=date.fromisoformat(as_of), peers_fn=lambda _t: _async(scaled),
                                       frame_fn=src.frame, ciks_fn=lambda: ciks)

    state.update(await verify(state, today=date.fromisoformat(as_of), fetch_peers=peers_then))
    if state.get("verdict") == "not_covered":
        return {"status": "not_covered", "reason": state.get("not_covered_reason")}
    # News, economic fit and geography are left out by design, so completeness is not
    # allowed to lower confidence or withhold anything in the replay.
    state["completeness_detail"] = {**(state.get("completeness_detail") or {}), "decision": "proceed"}
    state.update(analyse(state))
    card = state.get("scorecard") or {}
    verdict, conf = state.get("preliminary_verdict"), state.get("preliminary_confidence")
    peers = (state.get("valuation") or {}).get("peers") or {}
    reasons: dict[str, int] = {}
    for e in peers.get("excluded") or []:
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    peer_check = {"candidates": len(peers.get("peers") or []) + len(peers.get("excluded") or []),
                  "kept": len(peers.get("peers") or []), "excluded": reasons,
                  "used": {m: c.get("peers_used") for m, c in (peers.get("comparisons") or {}).items()},
                  "not_computed": peers.get("not_computed") or {}}
    out = {"status": "ok", "verdict": verdict, "confidence": conf, "score": card.get("score"), "peer_check": peer_check,
           "sub_scores": state.get("sub_scores"), "low_reliability": card.get("low_reliability"),
           "latest_fiscal_year": max((r.get("fiscal_year") or 0 for r in state["fundamentals_records"]), default=0)
           or None}
    if verdict not in scoring.SCORED_VERDICTS:
        return {**out, "status": "no_verdict"}
    result = scoring.excess_return(rows, bench, as_of, scoring.config()["horizon_months"])
    if result is None:
        return {**out, "status": "no_prices_after"}
    return {**out, **result, "correct": scoring.is_correct(verdict, result["excess"])}


async def _async(value):
    return value


async def market_value_on(src: Sources, ticker: str, day: str) -> float | None:
    """Today's market value moved back to `day` by the price change since (shares are
    taken as unchanged, so heavy buybacks or issues add a small error)."""
    rows = await src.prices(ticker)
    now_cap = await src.market_cap(ticker)
    then, now = adj_close_on(rows, day), (rows[-1]["adj_close"] if rows else None)
    return now_cap * then / now if now_cap and then and now else None


async def select_by_date(companies: list[str], dates: list[str], top: int | None, src: Sources,
                         progress=print) -> dict[str, list[str]]:
    """{date: the `top` largest companies by market value on that date}. Without `top`
    (or with fewer candidates), every company is replayed on every date."""
    if not top or len(companies) <= top:
        return {d: list(companies) for d in dates}
    values: dict[str, dict[str, float]] = {d: {} for d in dates}
    for i, t in enumerate(companies, 1):
        for d in dates:
            try:
                v = await market_value_on(src, t, d)
            except Exception as err:            # noqa: BLE001 - not priced: cannot be selected
                logger.warning("No market value for %s: %s", t, err)
                v = None
            if v:
                values[d][t] = v
        if i % 10 == 0:
            progress(f"  market values: {i}/{len(companies)}")
    return {d: [t for t, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:top]] for d, vals in values.items()}


async def sector_peers(ticker: str, u: dict, listed: list[dict], src: Sources) -> list[dict]:
    """Same-sector companies from the replay list that FMP did not list, closest in today's
    market value first, marked sector_peer (version 2)."""
    sector = u.get("sector_of", {}).get(ticker)
    have = {p["symbol"] for p in listed} | {ticker}
    names = [t for t, sec in u.get("sector_of", {}).items() if sec == sector and t not in have]
    if not names:
        return []
    own = await _cap(src, ticker)
    rows = []
    for t in names:
        value = await _cap(src, t)
        if value:
            rows.append({"symbol": t, "name": t, "market_value": value, "sector_peer": True})
    if own:
        rows.sort(key=lambda r: abs(math.log(r["market_value"] / own)))
    return rows


async def _cap(src: Sources, symbol: str) -> float | None:
    try:
        return await src.market_cap(symbol)
    except Exception as err:                    # noqa: BLE001 - a missing value only drops this peer
        logger.info("No market value for %s: %s", symbol, err)
        return None


# --- The whole run ----------------------------------------------------------------------------

def _done(conn: sqlite3.Connection, version: int) -> set[tuple[str, str]]:
    """Company and date pairs already replayed. A "failed" one (a dropped connection, a
    download error) is not counted, so the next run tries it again."""
    return {(r["ticker"], r["as_of"]) for r in
            conn.execute("SELECT ticker, as_of FROM replay_scores WHERE universe_version = ? AND status != 'failed'",
                         (version,))}


def _save(conn: sqlite3.Connection, version: int, ticker: str, as_of: str, r: dict) -> None:
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO replay_scores (universe_version, ticker, as_of, status, verdict, "
                     "confidence, score, excess_return, correct, result_json, scored_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (version, ticker, as_of, r["status"], r.get("verdict"), r.get("confidence"), r.get("score"),
                      r.get("excess"), None if r.get("correct") is None else int(r["correct"]),
                      json.dumps(r, default=str), utc_now_iso()))


async def run(conn: sqlite3.Connection | None = None, src: Sources | None = None, limit: int | None = None,
              progress=print) -> dict:
    conn = conn or connect()
    conn.executescript(SCHEMA)
    src = src or Sources()
    u = universe()
    version, dates, profile = u["version"], u["dates"], u["profile"]
    companies = u["companies"][:limit] if limit else u["companies"]
    done = _done(conn, version)
    ciks = await src.ciks()
    bench = await src.prices(u["benchmark"])
    # Version 5: on each date only the largest companies by market value on that date are
    # replayed (select_top), so the list is not chosen with hindsight.
    selected = await select_by_date(companies, dates, u.get("select_top"), src, progress)
    CacheStore(conn).set("replay_selection", str(version), selected, ttl=timedelta(days=365))

    for i, ticker in enumerate(companies, 1):
        todo = [d for d in dates if (ticker, d) not in done and ticker in selected.get(d, [])]
        if not todo:
            continue
        cik = ciks.get(ticker)
        if not cik:
            for d in todo:
                _save(conn, version, ticker, d, {"status": "no_cik"})
            continue
        try:
            facts = await src.company_facts(cik)
            rows = await src.prices(ticker)
            peers_now = await src.peer_list(ticker)
            peers_now += await sector_peers(ticker, u, peers_now, src)
            peer_rows, no_prices = {}, []
            for p in peers_now:
                try:
                    peer_rows[p["symbol"]] = await src.prices(p["symbol"])
                except Exception as err:        # noqa: BLE001 - a peer without prices is left out
                    logger.warning("No prices for peer %s: %s", p["symbol"], err)
                    no_prices.append(p["symbol"])
        except Exception as err:                # noqa: BLE001 - one company's failure is recorded, not fatal
            logger.warning("Replay of %s failed: %s", ticker, err)
            for d in todo:
                _save(conn, version, ticker, d, {"status": "failed", "reason": repr(err)})
            continue
        for d in todo:
            try:
                r = await replay_one(ticker, cik, d, facts=facts, rows=rows, bench=bench, peers_now=peers_now,
                                     peer_rows=peer_rows, src=src, ciks=ciks, profile=profile)
            except Exception as err:            # noqa: BLE001
                logger.warning("Replay of %s on %s failed: %s", ticker, d, err)
                r = {"status": "failed", "reason": repr(err), "where": traceback.format_exc().splitlines()[-3:]}
            if r.get("peer_check") is not None:
                r["peer_check"]["no_prices"] = no_prices
                r["peer_check"]["sector_peers_added"] = sum(1 for p in peers_now if p.get("sector_peer"))
            _save(conn, version, ticker, d, r)
        progress(f"[{i}/{len(companies)}] {ticker} done")
    return report(conn, version)


PARTS = ("business_quality", "valuation", "forecast")
PART_NAMES = {"business_quality": "Business quality", "valuation": "Valuation", "forecast": "Forecast"}


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 6) if xs else None


def what_predicted(scored: list[dict]) -> dict:
    """For each sub-score: the average excess return of stocks scoring +1 or more, of those
    scoring -1 or less, and in between. A sub-score that helps shows high above low."""
    out = {}
    for part in PARTS:
        groups = {"high": [], "middle": [], "low": []}
        for r in scored:
            v = (r.get("sub_scores") or {}).get(part)
            if v is None:
                continue
            groups["high" if v >= 1 else "low" if v <= -1 else "middle"].append(r["excess"])
        out[part] = {g: {"count": len(xs), "average_excess": _avg(xs)} for g, xs in groups.items()}
        hi, lo = out[part]["high"]["average_excess"], out[part]["low"]["average_excess"]
        out[part]["high_minus_low"] = round(hi - lo, 6) if hi is not None and lo is not None else None
    return out


# Weighted score bands for score_bands(): where should the lines sit? (7 October, after the
# spread check found weak companies at -0.5 to -0.6 staying Neutral.)
BANDS = [(None, -0.75), (-0.75, -0.5), (-0.5, -0.25), (-0.25, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, None)]


def score_bands(scored: list[dict]) -> list[dict]:
    """The average excess return of the stocks in each weighted-score band. If the -0.75 to
    -0.5 band trails the market like the unfavourable band does, moving the unfavourable line
    to -0.5 is supported; if it does not, it is not."""
    out = []
    for lo, hi in BANDS:
        xs = [r["excess"] for r in scored if r.get("score") is not None
              and (lo is None or r["score"] > lo) and (hi is None or r["score"] <= hi)]
        label = (f"{hi:+.2f} or below" if lo is None else f"above {lo:+.2f}" if hi is None
                 else f"{lo:+.2f} to {hi:+.2f}")
        out.append({"band": label, "count": len(xs), "average_excess": _avg(xs),
                    "beat_market": round(sum(1 for x in xs if x > 0) / len(xs), 4) if xs else None})
    return out


def by_year(scored: list[dict]) -> dict:
    """The verdict averages for each replay date: does the pattern hold every year?"""
    out = {}
    for day in sorted({r["as_of"] for r in scored}):
        rows = [r for r in scored if r["as_of"] == day]
        out[day] = {v: {"count": len(xs), "average_excess": _avg(xs)}
                    for v in scoring.SCORED_VERDICTS
                    for xs in [[r["excess"] for r in rows if r["verdict"] == v]]}
    return out


def report(conn: sqlite3.Connection, version: int | None = None) -> dict:
    conn.executescript(SCHEMA)
    version = version or universe()["version"]
    rows = [{**json.loads(r["result_json"]), "ticker": r["ticker"], "as_of": r["as_of"]}
            for r in conn.execute("SELECT ticker, as_of, result_json FROM replay_scores WHERE universe_version = ?",
                                  (version,))]
    scored = [r for r in rows if r.get("status") == "ok"]
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    verdicts: dict[str, int] = {}
    for r in scored:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    s = scoring.summarise(scored)
    # Verdicts reached without a valuation (fewer than 5 usable peers even after the top-up)
    # rest on business quality alone; counted apart so they can be judged on their own.
    no_prices = sorted({sym for r in scored for sym in (r.get("peer_check") or {}).get("no_prices", [])})
    bare = [r for r in scored if (r.get("sub_scores") or {}).get("valuation") is None]
    without = {"count": len(bare), "summary": scoring.summarise(bare) if bare else None}
    selection = CacheStore(conn).get("replay_selection", str(version)) or {}
    return {"universe_version": version, "statuses": statuses, "verdict_counts": verdicts, "summary": s,
            "selection": {d: len(v) for d, v in selection.items()},
            "what_predicted": what_predicted(scored), "by_year": by_year(scored),
            "score_bands": score_bands(scored),
            "without_valuation": without, "peers_without_prices": no_prices,
            "calibration": calibration.check(s["by_confidence"]), "tuning": tuning.suggest(scored),
            "cautions": CAUTIONS}


def text(rep: dict) -> list[str]:
    s = rep["summary"]
    lines = [f"Replay (universe version {rep['universe_version']})",
             "Results: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["statuses"].items())),
             *([("Companies replayed on each date (the largest then): "
                 + ", ".join(f"{d} {n}" for d, n in sorted(rep["selection"].items())))] if rep.get("selection") else []),
             "Verdicts: " + (", ".join(f"{k} {v}" for k, v in sorted(rep["verdict_counts"].items())) or "none")]
    if s["count"]:
        lines.append(f"Scored: {s['count']}, right {s['hit_rate'] * 100:.0f}% of the time")
        for name, row in s["by_verdict"].items():
            if row["count"]:
                lines.append(f"  {name}: {row['count']}, average excess return {row['average_excess'] * 100:+.1f}% "
                             f"against the S&P 500, right {row['hit_rate'] * 100:.0f}%")
        for name, row in s["by_confidence"].items():
            if row["count"]:
                lines.append(f"  {name} confidence: {row['count']}, right {row['hit_rate'] * 100:.0f}%")
        if s["verdicts_in_order"] is not None:
            lines.append("  Favourable above neutral above unfavourable: " + ("yes" if s["verdicts_in_order"] else "no"))
    wp = rep.get("what_predicted") or {}
    if wp:
        lines.append("What predicted what (average excess return: sub-score +1 or more / in between / -1 or less):")
        for part, g in wp.items():
            cells = []
            for k in ("high", "middle", "low"):
                a = g[k]["average_excess"]
                cells.append(f"{a * 100:+.1f}% ({g[k]['count']})" if a is not None else f"n/a ({g[k]['count']})")
            gap = g.get("high_minus_low")
            lines.append(f"  {PART_NAMES[part]}: {' / '.join(cells)}"
                         + (f"; high minus low {gap * 100:+.1f} points" if gap is not None else ""))
    bands = rep.get("score_bands") or []
    if bands:
        lines.append("By weighted score (average excess return, share that beat the S&P 500):")
        for b in bands:
            lines.append(f"  {b['band']}: " + (f"{b['average_excess'] * 100:+.1f}%, {b['beat_market'] * 100:.0f}% beat it "
                                                f"({b['count']})" if b["count"] else "none"))
    years = rep.get("by_year") or {}
    if years:
        lines.append("By date (average excess return: favourable / neutral / unfavourable):")
        for day, g in years.items():
            cells = []
            for v in scoring.SCORED_VERDICTS:
                a = g[v]["average_excess"]
                cells.append(f"{a * 100:+.1f}% ({g[v]['count']})" if a is not None else f"n/a ({g[v]['count']})")
            lines.append(f"  {day}: {' / '.join(cells)}")
    wv = rep.get("without_valuation") or {}
    if wv.get("count"):
        ws = wv["summary"]
        lines.append(f"Without a valuation (business quality only): {wv['count']} of {s['count']}"
                     + (f", right {ws['hit_rate'] * 100:.0f}%" if ws and ws["hit_rate"] is not None else ""))
    if rep.get("peers_without_prices"):
        missing = rep["peers_without_prices"]
        lines.append(f"Peers with no Yahoo prices (left out): {len(missing)}: {', '.join(missing[:15])}"
                     + (" ..." if len(missing) > 15 else ""))
    lines.append(f"Confidence: {rep['calibration']['finding']}")
    lines += [f"Tuning: {line}" for line in rep["tuning"]["lines"]]
    lines += [f"Caution: {c}" for c in rep["cautions"]]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay past dates for US companies (backtest Route B)")
    parser.add_argument("--limit", type=int, help="only the first N companies (a quick trial)")
    parser.add_argument("--report", action="store_true", help="print the report from saved results, no downloads")
    args = parser.parse_args()
    if args.report:
        rep = report(connect())
    else:
        rep = asyncio.run(run(limit=args.limit))
    from invest_agent.eval.backtest import save
    today = datetime.now(UTC).date().isoformat()
    path = save({**rep, "today": f"replay_v{rep['universe_version']}_{today}"})
    print("\n".join(text(rep)))
    print(f"Saved: {path}")


if __name__ == "__main__":
    main()
