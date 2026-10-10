"""
mcp_servers/market_data/metrics.py: turns up to five years of daily prices into the figures the analysis uses.

What this file does:
    Takes a stock's daily price rows (and its benchmark index's) and computes the
    figures in design 14.4:

      current price           the last actual traded close, with its date
      52-week high and low    and where today's price sits between them (0 = low, 1 = high)
      returns                 over 1, 3, 6 and 12 months
      moving averages         50-day and 200-day (context only, not trading signals)
      volatility              how much the price swings in a year, as a percentage
      beta                    how much it moves with the market (2 years of weekly returns)
      maximum drawdown        the worst fall from a peak to a later low
      liquidity               average shares and money traded a day, and the
                              "thinly traded" flag (below Rs 5 crore or $1 million a day)

    Adjusted versus actual prices: after a 10-for-1 split the actual price drops to a
    tenth overnight, which would look like a 90% crash. Adjusted prices rewrite the
    past so the history is comparable, so every figure except the current price and
    traded value uses adjusted closes.

    Plain code with no network calls, so it is fully unit-tested.
"""

from __future__ import annotations

import math
from typing import Any

# pandas: the standard Python library for tables and time series. A Series is one
# column of numbers with a date index; it has built-in rolling windows, percentage
# changes and weekly resampling, which is exactly what these figures need.
import pandas as pd

from invest_agent.common.yaml_loader import load_yaml

TRADING_DAYS = 252        # roughly the number of trading days in a year
WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252, "3y": 756, "5y": 1260}
CHART_WEEKS = 260         # five years of weekly closes for the report's price chart


def _series(rows: list[dict], column: str) -> pd.Series:
    """One column of the rows as a Series indexed by date (oldest first)."""
    index = pd.to_datetime([r["date"] for r in rows])
    return pd.Series([r.get(column) for r in rows], index=index, dtype="float64").sort_index()


def _clean(value: float | None, digits: int = 4) -> float | None:
    """Round, and turn "not a number" results into None so they never reach JSON as NaN."""
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(float(value), digits)


def _price_years_ago(adj: pd.Series, years: int, slack_days: int = 10) -> float | None:
    """The price on or before the same day `years` ago; the first price held if the
    history starts no more than `slack_days` after that day; otherwise None."""
    target = adj.index[-1] - pd.DateOffset(years=years)
    before = adj[adj.index <= target]
    if len(before):
        return float(before.iloc[-1])
    if (adj.index[0] - target).days <= slack_days:
        return float(adj.iloc[0])
    return None


def compute_price_metrics(rows: list[dict], benchmark_rows: list[dict] | None, currency: str) -> dict[str, Any]:
    """Compute every figure from daily rows of {date, close, adj_close, volume}.

    Returns {"metrics": {...}, "flags": [...]}. A figure that needs more history than
    the stock has is left out and flagged, never estimated.
    """
    cfg = load_yaml("mcp_servers/market_data/price_config.yaml")
    flags: list[str] = []
    m: dict[str, Any] = {}

    close = _series(rows, "close")
    adj = _series(rows, "adj_close").fillna(close)     # some sources only give one close
    volume = _series(rows, "volume").fillna(0)

    # Current price: the last actual (unadjusted) close.
    m["current_price"] = _clean(close.iloc[-1], 4)
    m["price_as_of"] = close.index[-1].date().isoformat()
    m["currency"] = currency

    # 52-week range, on adjusted closes so a split does not distort it.
    year = adj.tail(TRADING_DAYS)
    if len(adj) >= TRADING_DAYS * 0.9:
        high, low = year.max(), year.min()
        m["high_52w"], m["low_52w"] = _clean(high), _clean(low)
        m["position_in_52w_range"] = _clean((adj.iloc[-1] - low) / (high - low)) if high > low else None
    else:
        flags.append("under_one_year_of_history")

    # Returns: percentage change over each window, as a fraction (0.12 = +12%). Up to a
    # year by trading days; 3 and 5 years by calendar date, the last price on or before
    # the same day 3 or 5 years ago (a "5y" download starts on about that day, so counting
    # 1,260 trading days would just miss it).
    for name, days in WINDOWS.items():
        if name.endswith("y") and name != "12m":
            start = _price_years_ago(adj, int(name[:-1]))
            if start is not None:
                m[f"return_{name}"] = _clean(adj.iloc[-1] / start - 1)
        elif len(adj) > days:
            m[f"return_{name}"] = _clean(adj.iloc[-1] / adj.iloc[-1 - days] - 1)

    # Moving averages: .rolling(n).mean() is the average of each n-day window.
    for days in (50, 200):
        if len(adj) >= days:
            m[f"ma_{days}d"] = _clean(adj.rolling(days).mean().iloc[-1])

    # Volatility: standard deviation of daily returns over a year, scaled to a year
    # by the square root of 252 (the usual convention).
    daily = adj.pct_change().dropna()          # pct_change: each day's change from the day before
    if len(daily) >= 60:
        m["volatility_1y"] = _clean(daily.tail(TRADING_DAYS).std() * math.sqrt(TRADING_DAYS))
    else:
        flags.append("too_little_history_for_volatility")

    # Maximum drawdown: the worst fall from the highest point so far to a later low,
    # over the last two years (as before) and over all the history held (up to five).
    two = adj.tail(TRADING_DAYS * 2)
    m["max_drawdown_2y"] = _clean((two / two.cummax() - 1).min())
    # Five-year figures only when the history reaches back five years (see _price_years_ago).
    start5 = _price_years_ago(adj, 5)
    if start5 is not None:
        m["max_drawdown_5y"] = _clean((adj / adj.cummax() - 1).min())
        # Growth a year over five years (compound).
        m["price_cagr_5y"] = _clean((adj.iloc[-1] / start5) ** (1 / 5) - 1)

    # Beta: weekly returns of the stock against the benchmark, decided 4 October.
    beta_cfg = cfg["beta"]
    if benchmark_rows:
        bench = _series(benchmark_rows, "adj_close").fillna(_series(benchmark_rows, "close"))
        # The market index over the same five years, so the stock's growth can be set
        # beside it (the report's summary and share price section).
        bench5 = _price_years_ago(bench, 5)
        if bench5 is not None and "price_cagr_5y" in m:
            m["benchmark_return_5y"] = _clean(bench.iloc[-1] / bench5 - 1)
            m["benchmark_cagr_5y"] = _clean((bench.iloc[-1] / bench5) ** (1 / 5) - 1)
        # resample("W-FRI").last(): the last price of each week (weeks ending Friday).
        weekly = pd.concat({"stock": adj.resample("W-FRI").last(), "bench": bench.resample("W-FRI").last()},
                           axis=1).dropna().pct_change().dropna()
        weekly = weekly.tail(int(beta_cfg["window_years"] * 52))
        if len(weekly) >= beta_cfg["min_history_weeks"]:
            # beta = covariance(stock, market) / variance(market)
            m["beta"] = _clean(weekly["stock"].cov(weekly["bench"]) / weekly["bench"].var(), 3)
            m["beta_weeks"] = len(weekly)
        else:
            flags.append("too_little_history_for_beta")
    else:
        flags.append("no_benchmark_history")

    # Liquidity: shares traded, and money traded (actual price x shares), per day.
    traded_value = close * volume
    m["avg_volume_20d"] = _clean(volume.tail(20).mean(), 0)
    m["avg_volume_6m"] = _clean(volume.tail(126).mean(), 0)
    m["avg_traded_value_20d"] = _clean(traded_value.tail(20).mean(), 0)
    m["avg_traded_value_3m"] = _clean(traded_value.tail(63).mean(), 0)
    limit = cfg["thinly_traded"].get(currency)
    if limit is not None and m["avg_traded_value_3m"] is not None:
        m["thinly_traded"] = bool(m["avg_traded_value_3m"] < limit)

    # Weekly closes for the price chart: the stock and its benchmark, both from the same
    # starting week. Not records: a chart series, carried separately.
    # resample labels each week by its Friday, so a week still in progress would be dated
    # a few days ahead; each point is dated by its actual last trading day instead.
    weekly_stock = adj.resample("W-FRI").last().dropna().tail(CHART_WEEKS)
    last_day = adj.index.to_series().resample("W-FRI").last().reindex(weekly_stock.index)
    chart = {"dates": [d.date().isoformat() for d in pd.to_datetime(last_day.values)],
             "stock": [round(float(v), 4) for v in weekly_stock.values], "benchmark": None}
    if benchmark_rows:
        b = _series(benchmark_rows, "adj_close").fillna(_series(benchmark_rows, "close"))
        b = b.resample("W-FRI").last().reindex(weekly_stock.index).ffill()
        if b.notna().sum() >= len(b) * 0.9:
            chart["benchmark"] = [None if pd.isna(v) else round(float(v), 4) for v in b.values]
    chart["years"] = round(len(adj) / TRADING_DAYS, 1)
    return {"metrics": m, "flags": flags, "chart": chart}
