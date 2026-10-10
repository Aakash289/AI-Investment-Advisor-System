"""
tests/unit/test_replay.py: the backtest replay of past dates and the tuning check (build step 15b).

What this file checks:
    point in time    the SEC normaliser with as_of keeps only what was filed by that date;
                     a restatement filed later does not leak in
    splits           a past close is put back to the real price of the day (Yahoo divides
                     old closes by later splits)
    one replay       a fake company with six years of SEC facts, prices with a 4-for-1
                     split, and five peers: the live verification and analysis steps give
                     a verdict on each past date, scored on the next 12 months; a date before
                     enough was filed gives no verdict
    the run          results saved per company and date, a second run repeats nothing,
                     and the report lists the cautions
    tuning           nothing before 100 verdicts; with a clear pattern in the data the
                     separation is measured on a fixed 70/30 split

No network: every download is a fake, the database is in memory.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, timedelta

import pytest

from invest_agent.eval import replay, tuning
from invest_agent.mcp_servers.filings.normaliser import normalise_company_facts
from invest_agent.storage.db import connect
from tests.unit.test_fundamentals_normaliser import annual, facts_fixture

# --- Point in time and splits ------------------------------------------------------------------

def test_only_what_was_filed_by_the_date():
    now = normalise_company_facts(facts_fixture())
    then = normalise_company_facts(facts_fixture(), as_of="2024-12-31")
    assert now["revenue"][0]["fiscal_year"] == 2025 and then["revenue"][0]["fiscal_year"] == 2024
    fy2024_now = next(r for r in now["revenue"] if r["fiscal_year"] == 2024)["value"]
    fy2024_then = next(r for r in then["revenue"] if r["fiscal_year"] == 2024)["value"]
    assert fy2024_now == 999.0 and fy2024_then == 110.0 * 2024      # the 2025 restatement was not known yet


def test_past_close_put_back_before_a_split():
    rows = [{"date": "2024-01-02", "close": 50.0, "adj_close": 50.0, "split": 0.0},
            {"date": "2024-06-10", "close": 55.0, "adj_close": 55.0, "split": 4.0},
            {"date": "2024-07-01", "close": 60.0, "adj_close": 60.0, "split": 0.0}]
    then = replay.until(rows, "2024-03-01")
    assert then == [{"date": "2024-01-02", "close": 200.0, "adj_close": 50.0, "split": 0.0}]
    assert replay.until(rows, "2024-06-30")[-1]["close"] == 55.0


# --- A fake company ---------------------------------------------------------------------------

def fake_facts(first_year=2016, last_year=2024, revenue0=40e9, growth=0.10):
    """Six-plus years of a steady US company, each 10-K filed in early November."""
    g: dict = {}

    def put(tag, unit, val, end, filed, start=None):
        g.setdefault(tag, {"units": {unit: []}})["units"][unit].append(annual(val, end, filed, start=start))

    for y in range(first_year, last_year + 1):
        rev = revenue0 * (1 + growth) ** (y - first_year)
        start, end, filed = f"{y - 1}-10-01", f"{y}-09-30", f"{y}-11-01"
        put("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", rev, end, filed, start)
        put("OperatingIncomeLoss", "USD", rev * 0.25, end, filed, start)
        put("NetIncomeLoss", "USD", rev * 0.20, end, filed, start)
        put("IncomeTaxExpenseBenefit", "USD", rev * 0.04, end, filed, start)
        put("InterestExpense", "USD", rev * 0.01, end, filed, start)
        put("DepreciationDepletionAndAmortization", "USD", rev * 0.05, end, filed, start)
        put("NetCashProvidedByUsedInOperatingActivities", "USD", rev * 0.28, end, filed, start)
        put("PaymentsToAcquirePropertyPlantAndEquipment", "USD", rev * 0.06, end, filed, start)
        put("StockholdersEquity", "USD", rev * 0.8, end, filed)
        put("LongTermDebtNoncurrent", "USD", rev * 0.3, end, filed)
        put("CashAndCashEquivalentsAtCarryingValue", "USD", rev * 0.2, end, filed)
        put("EarningsPerShareDiluted", "USD/shares", rev * 0.20 / 1e9, end, filed, start)
    shares = {"units": {"shares": [annual(1e9, f"{y}-10-20", f"{y}-11-01") for y in range(first_year, last_year + 1)]}}
    return {"facts": {"us-gaap": g, "dei": {"EntityCommonStockSharesOutstanding": shares}}}


def price_rows(start="2015-01-01", end="2026-09-30", p0=100.0, daily=0.0006, split_on=None, ratio=4.0):
    rows, d, p = [], date.fromisoformat(start), p0
    stop = date.fromisoformat(end)
    i = 0
    while d <= stop:
        if d.weekday() < 5:
            p *= 1 + daily + (0.01 if i % 2 else -0.01)
            i += 1
            rows.append({"date": d.isoformat(), "close": p, "adj_close": p, "volume": 1e7,
                         "split": ratio if split_on and d.isoformat() == split_on else 0.0})
        d += timedelta(days=1)
    if split_on:                                    # Yahoo shows earlier closes divided by the split
        for r in rows:
            if r["date"] < split_on:
                r["close"] /= ratio
                r["adj_close"] /= ratio
    return rows


PEERS = [f"P{i}" for i in range(1, 6)]


class FakeSources(replay.Sources):
    def __init__(self):
        self.fetched: list[str] = []

    async def company_facts(self, cik):
        self.fetched.append(f"facts:{cik}")
        return fake_facts()

    async def prices(self, symbol):
        self.fetched.append(f"prices:{symbol}")
        if symbol == "^GSPC":
            return price_rows(daily=0.0004)
        if symbol == "AAA":
            return price_rows(p0=30.0, split_on="2024-06-10")
        return price_rows(p0=50.0, daily=0.0005)

    async def peer_list(self, ticker):
        return [{"symbol": s, "name": s, "market_value": 2e11} for s in PEERS]

    async def frame(self, tag, unit, period, taxonomy="us-gaap"):
        values = {"RevenueFromContractWithCustomerExcludingAssessedTax": 6e10, "NetIncomeLoss": 8e9,
                  "OperatingIncomeLoss": 1.2e10, "DepreciationDepletionAndAmortization": 3e9,
                  "StockholdersEquity": 5e10, "LongTermDebtNoncurrent": 2e10,
                  "CashAndCashEquivalentsAtCarryingValue": 1e10}
        return {str(100 + i): values[tag] for i in range(1, 6)} if tag in values else {}

    async def ciks(self):
        return {"AAA": "100", "BBB": "200", **{s: str(100 + i) for i, s in enumerate(PEERS, 1)}}

    async def market_cap(self, symbol):
        return {"AAA": 3e11, "BBB": 2e11}.get(symbol, 1e11)


def test_one_company_replayed_on_two_dates(monkeypatch):
    monkeypatch.setattr(replay, "universe", lambda: {"version": 9, "dates": ["2022-03-31", "2025-03-31"],
                                                     "benchmark": "^GSPC", "companies": ["AAA"],
                                                     "profile": {"risk_tolerance": "medium", "horizon_years": 10,
                                                                 "goal": "growth", "budget_amount": 1e6,
                                                                 "budget_currency": "USD"}})
    conn, src = connect(":memory:"), FakeSources()
    rep = asyncio.run(replay.run(conn, src, progress=lambda *_: None))
    rows = {r["as_of"]: r for r in conn.execute("SELECT as_of, status, verdict, excess_return, result_json FROM replay_scores")}
    assert set(rows) == {"2022-03-31", "2025-03-31"}
    for day, r in rows.items():
        assert r["status"] == "ok", (day, r["result_json"])
        assert r["verdict"] in ("favourable", "neutral", "unfavourable") and r["excess_return"] is not None
    assert rep["summary"]["count"] == 2 and rep["cautions"][0].startswith("Survivorship")

    # Running again repeats nothing.
    src.fetched.clear()
    asyncio.run(replay.run(conn, src, progress=lambda *_: None))
    assert [f for f in src.fetched if f.startswith("facts")] == []


def test_a_date_before_enough_was_filed_gives_no_verdict():
    src = FakeSources()

    async def go():
        bench = await src.prices("^GSPC")
        return await replay.replay_one(
            "AAA", "100", "2017-03-31", facts=fake_facts(), rows=await src.prices("AAA"), bench=bench,
            peers_now=await src.peer_list("AAA"), peer_rows={p: await src.prices(p) for p in PEERS}, src=src,
            ciks=await src.ciks(), profile={"goal": "growth", "horizon_years": 10, "risk_tolerance": "medium"})
    r = asyncio.run(go())
    assert r["status"] == "no_verdict" and r["verdict"] == "insufficient_data"


# --- Tuning -----------------------------------------------------------------------------------

def test_tuning_waits_for_enough_verdicts():
    out = tuning.suggest([{"sub_scores": {"business_quality": 1.0}, "excess": 0.1, "ticker": "A", "as_of": "x"}] * 10)
    assert out["status"] == "not_enough_data" and "10 of the 100" in out["lines"][0]


def test_tuning_measures_separation_on_a_fixed_split():
    rng = random.Random(7)
    rows = []
    for i in range(200):
        quality = rng.uniform(-2, 2)
        rows.append({"ticker": f"T{i}", "as_of": "2023-03-31",
                     "sub_scores": {"business_quality": quality, "valuation": rng.uniform(-2, 2), "forecast": 0.0},
                     "low_reliability": [], "excess": 0.05 * quality + rng.gauss(0, 0.02)})
    a, b = tuning.suggest(rows), tuning.suggest(rows)
    assert a["lines"] == b["lines"] and a["status"] in ("suggestion", "keep")
    assert a["lines"][0].startswith("today's settings")
    best = a["best"]
    assert best["train"] > 0 and best["weights"] in tuning.WEIGHT_SETS


def test_short_peer_list_topped_up_from_the_same_sector():
    class FewPeers(FakeSources):
        async def peer_list(self, ticker):
            return [{"symbol": "P1", "name": "P1", "market_value": 2e11}]
    u = {"sector_of": {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"}}
    extra = asyncio.run(replay.sector_peers("AAA", u, [{"symbol": "P1"}], FewPeers()))
    assert [p["symbol"] for p in extra] == ["BBB"] and extra[0]["sector_peer"] and extra[0]["market_value"] == 2e11


def test_no_valuation_lowers_confidence():
    from invest_agent.analysis.confidence import assess
    strong = {"business_quality": 1.5, "valuation": None, "macro_fit": 0.2, "news": 0.1, "forecast": 0.3}
    out = assess(strong, "favourable", completeness_decision="proceed")
    assert out["level"] == "medium" and "no valuation against similar companies" in out["reasons"][-1]
    with_val = assess({**strong, "valuation": 0.5, "forecast": 0.5}, "favourable", completeness_decision="proceed")
    assert with_val["level"] == "high"


def test_yahoo_rate_limits_are_retried_and_do_not_mark_yahoo_down(monkeypatch):
    from invest_agent.observability import health_monitor
    monkeypatch.setattr(replay, "YAHOO_WAITS", [0, 0, 0])
    tries = []

    def flaky(symbol):
        tries.append(symbol)
        if len(tries) < 3:
            raise RuntimeError("Too Many Requests. Rate limited.")
        return [{"date": "2025-01-02", "close": 1.0, "adj_close": 1.0, "split": 0.0}]

    rows = asyncio.run(replay._yahoo("history_10y", flaky, "AAPL"))
    assert len(tries) == 3 and rows[0]["close"] == 1.0
    assert not health_monitor.is_down("yfinance")          # the live Yahoo source is untouched


def test_failed_results_are_tried_again():
    conn = connect(":memory:")
    conn.executescript(replay.SCHEMA)
    replay._save(conn, 9, "AAA", "2024-03-31", {"status": "failed", "reason": "connection dropped"})
    replay._save(conn, 9, "BBB", "2024-03-31", {"status": "ok", "verdict": "neutral"})
    assert replay._done(conn, 9) == {("BBB", "2024-03-31")}


def test_no_figures_is_reported_not_crashed():
    src = FakeSources()

    async def go():
        bench = await src.prices("^GSPC")
        return await replay.replay_one("AAA", "100", "2024-03-31", facts={"facts": {"us-gaap": {"Revenues2": {}}}},
                                       rows=await src.prices("AAA"), bench=bench, peers_now=[], peer_rows={},
                                       src=src, ciks=await src.ciks(), profile={})
    r = asyncio.run(go())
    assert r["status"] == "no_figures" and r["revenue_like_tags"] == ["Revenues2"]


def test_what_predicted_and_by_year():
    scored = [{"as_of": "2023-03-31", "verdict": "favourable", "excess": 0.10,
               "sub_scores": {"business_quality": 1.5, "valuation": -2.0, "forecast": 0}},
              {"as_of": "2023-03-31", "verdict": "neutral", "excess": -0.05,
               "sub_scores": {"business_quality": -1.0, "valuation": 1.0, "forecast": None}},
              {"as_of": "2024-03-31", "verdict": "favourable", "excess": 0.02,
               "sub_scores": {"business_quality": 0.5, "valuation": 2.0, "forecast": 1}}]
    wp = replay.what_predicted(scored)
    assert wp["business_quality"]["high_minus_low"] == pytest.approx(0.15)
    assert wp["valuation"]["high"]["count"] == 2 and wp["valuation"]["low"]["average_excess"] == pytest.approx(0.10)
    years = replay.by_year(scored)
    assert years["2023-03-31"]["favourable"]["count"] == 1 and years["2024-03-31"]["neutral"]["count"] == 0
    lines = replay.text({"universe_version": 4, "statuses": {"ok": 3}, "verdict_counts": {"favourable": 2},
                         "summary": replay.scoring.summarise([{**r, "confidence": "high", "correct": True}
                                                              for r in scored]),
                         "what_predicted": wp, "by_year": years, "calibration": {"finding": "x"},
                         "tuning": {"lines": ["y"]}, "cautions": []})
    assert any(line.startswith("  Business quality:") for line in lines)


def test_tuning_never_suggests_a_setting_that_loses():
    rng = random.Random(3)
    rows = [{"ticker": f"T{i}", "as_of": "2023-03-31", "low_reliability": [],
             "sub_scores": {"business_quality": q, "valuation": rng.uniform(-2, 2), "forecast": 0.0},
             "excess": -0.05 * q + rng.gauss(0, 0.02)}                 # good scores did worse: nothing works
            for i, q in enumerate(rng.uniform(-2, 2) for _ in range(200))]
    out = tuning.suggest(rows)
    assert out["status"] != "suggestion"


def test_largest_on_each_date_are_selected():
    class Sizes(FakeSources):
        async def prices(self, symbol):
            # BIG halves in price after 2023; SMALL triples: their order flips between dates.
            if symbol == "BIG":
                return [{"date": "2022-03-31", "adj_close": 100.0}, {"date": "2025-03-31", "adj_close": 50.0}]
            if symbol == "SMALL":
                return [{"date": "2022-03-31", "adj_close": 10.0}, {"date": "2025-03-31", "adj_close": 30.0}]
            return [{"date": "2022-03-31", "adj_close": 10.0}, {"date": "2025-03-31", "adj_close": 10.0}]

        async def market_cap(self, symbol):
            return {"BIG": 100.0, "SMALL": 150.0, "TINY": 1.0}[symbol]

    sel = asyncio.run(replay.select_by_date(["BIG", "SMALL", "TINY"], ["2022-03-31", "2025-03-31"], 1, Sizes(),
                                            progress=lambda *_: None))
    assert sel == {"2022-03-31": ["BIG"], "2025-03-31": ["SMALL"]}       # BIG was 200 then, SMALL 50


def test_score_bands():
    from invest_agent.eval.replay import score_bands

    scored = [{"score": -0.9, "excess": -0.10}, {"score": -0.6, "excess": -0.05}, {"score": -0.55, "excess": 0.01},
              {"score": 0.0, "excess": 0.02}, {"score": 0.9, "excess": 0.03}, {"score": None, "excess": 0.5}]
    bands = {b["band"]: b for b in score_bands(scored)}
    assert bands["-0.75 or below"]["count"] == 1
    assert bands["-0.75 to -0.50"]["count"] == 2 and bands["-0.75 to -0.50"]["average_excess"] == -0.02
    assert bands["-0.75 to -0.50"]["beat_market"] == 0.5
    assert bands["above +0.75"]["count"] == 1 and bands["+0.25 to +0.50"]["count"] == 0
