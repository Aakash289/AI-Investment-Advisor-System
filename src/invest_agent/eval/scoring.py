"""
eval/scoring.py: was a past verdict right? (design 14.17)

What this file does:
    A verdict is judged by what the shares did over the next 12 months compared with
    the market, not by whether they went up: in a year when everything rose 20%, a
    stock that rose 5% was a poor choice.

      excess_return()    the stock's 12-month return (adjusted prices, so dividends and
                         splits count) minus its benchmark's (S&P 500 for US stocks,
                         Nifty 50 for Indian ones) over the same days, in the stock's
                         own currency
      is_correct()       favourable: excess above 0; unfavourable: below 0; neutral:
                         within +-10% (backtest_config.yaml)
      summarise()        the main measure, the average excess return for each verdict
                         (a working system shows favourable above neutral above
                         unfavourable), plus the share right overall and by confidence

    A right-or-wrong count is noisy, so the averages by verdict matter more than the hit
    rate. Insufficient data and not covered verdicts are not scored.

    Plain functions on price rows ({date, adj_close}); fetching the prices is
    backtest.py's job.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean

from invest_agent.common.yaml_loader import load_yaml

SCORED_VERDICTS = ("favourable", "neutral", "unfavourable")


def config() -> dict:
    return load_yaml("eval/backtest_config.yaml")


def _price_on_or_after(rows: list[dict], day: str) -> tuple[str, float] | None:
    return next(((r["date"], r["adj_close"]) for r in rows if r["date"] >= day), None)


def _price_on_or_before(rows: list[dict], day: str) -> tuple[str, float] | None:
    return next(((r["date"], r["adj_close"]) for r in reversed(rows) if r["date"] <= day), None)


def window(start: str, months: int) -> tuple[str, str]:
    """The start date and the date `months` later (365 days for 12 months)."""
    d0 = date.fromisoformat(start[:10])
    return d0.isoformat(), (d0 + timedelta(days=round(months * 365 / 12))).isoformat()


def period_return(rows: list[dict], start: str, end: str) -> dict | None:
    """Adjusted-price return from the first trading day on or after `start` to the last
    on or before `end`; None when the history does not cover the window."""
    rows = sorted(rows, key=lambda r: r["date"])
    a, b = _price_on_or_after(rows, start), _price_on_or_before(rows, end)
    if not a or not b or a[0] > b[0] or a[1] <= 0:
        return None
    return {"from": a[0], "to": b[0], "return": b[1] / a[1] - 1}


def excess_return(stock: list[dict], benchmark: list[dict], start: str, months: int | None = None) -> dict | None:
    """{"stock", "benchmark", "excess", "from", "to"}; None when either history falls
    short (a delisted stock is scored to its last trading day by the caller)."""
    months = months or config()["horizon_months"]
    d0, d1 = window(start, months)
    s, b = period_return(stock, d0, d1), period_return(benchmark, d0, d1)
    if s is None or b is None:
        return None
    return {"stock": round(s["return"], 6), "benchmark": round(b["return"], 6),
            "excess": round(s["return"] - b["return"], 6), "from": s["from"], "to": s["to"]}


def is_correct(verdict: str, excess: float, neutral_band: float | None = None) -> bool | None:
    band = config()["neutral_band"] if neutral_band is None else neutral_band
    if verdict == "favourable":
        return excess > 0
    if verdict == "unfavourable":
        return excess < 0
    if verdict == "neutral":
        return abs(excess) <= band
    return None


def summarise(scored: list[dict]) -> dict:
    """scored: [{"verdict", "confidence", "excess", "correct"}]. Averages by verdict, hit
    rates overall and by confidence, and whether the averages come in the right order."""
    rows = [r for r in scored if r.get("verdict") in SCORED_VERDICTS and r.get("excess") is not None]
    by_verdict = {}
    for v in SCORED_VERDICTS:
        xs = [r["excess"] for r in rows if r["verdict"] == v]
        by_verdict[v] = {"count": len(xs), "average_excess": round(mean(xs), 6) if xs else None,
                         "hit_rate": round(mean(1.0 if r["correct"] else 0.0 for r in rows if r["verdict"] == v), 4)
                         if xs else None}
    by_confidence = {}
    for c in ("high", "medium", "low"):
        hits = [1.0 if r["correct"] else 0.0 for r in rows if r.get("confidence") == c]
        by_confidence[c] = {"count": len(hits), "hit_rate": round(mean(hits), 4) if hits else None}
    avgs = [by_verdict[v]["average_excess"] for v in SCORED_VERDICTS]
    ordered = None if None in avgs else avgs[0] > avgs[1] > avgs[2]
    return {"count": len(rows),
            "hit_rate": round(mean(1.0 if r["correct"] else 0.0 for r in rows), 4) if rows else None,
            "by_verdict": by_verdict, "by_confidence": by_confidence, "verdicts_in_order": ordered}
