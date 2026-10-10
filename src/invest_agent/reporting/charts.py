"""
reporting/charts.py: the three charts in the report (design 14.14).

What this file does:
    Draws, from the report data object only, so a chart can never show a number the
    analysis did not have:
      revenue      six years of revenue as bars
      margins      gross (where reported), operating and net margin over six years
      fair value   the low, base and high fair value a year out against today's price,
                   shaded and labelled as a projection

    matplotlib draws each one; it is saved as a PNG at 200 dots per inch in memory and
    handed to the PDF. (The design asked for vector images; ReportLab cannot place
    matplotlib's vector output without another library, and at this resolution the
    difference does not show in print.)

    Style: muted colours, one accent (deep blue), light grid, no chart junk.
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")                      # draw to memory, no window
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

ACCENT = "#1f3a5f"
MUTED = "#8a9bb0"
LIGHT = "#d9e1ea"
FONT = {"family": "DejaVu Sans", "size": 8}


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=LIGHT, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=7)


def revenue_chart(years: list[str], values: list[float | None], unit_label: str, scale: float) -> bytes | None:
    pts = [(y, v / scale) for y, v in zip(years, values) if v is not None]
    if len(pts) < 2:
        return None
    plt.rc("font", **FONT)
    fig, ax = plt.subplots(figsize=(3.4, 2.0))
    ax.bar([p[0] for p in pts], [p[1] for p in pts], color=ACCENT, width=0.6)
    ax.set_title(f"Revenue ({unit_label})", fontsize=8, loc="left")
    _style(ax)
    return _png(fig)


def margin_chart(years: list[str], series: dict[str, list[float | None]]) -> bytes | None:
    lines = [("Gross", "gross_margin", MUTED, ":"), ("Operating", "operating_margin", ACCENT, "-"),
             ("Net", "net_margin", "#4f7cac", "--")]
    plt.rc("font", **FONT)
    fig, ax = plt.subplots(figsize=(3.4, 2.0))
    drawn = 0
    for label, key, colour, style in lines:
        pts = [(y, v * 100) for y, v in zip(years, series.get(key) or []) if v is not None]
        if len(pts) >= 2:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], style, color=colour, label=label, linewidth=1.4)
            drawn += 1
    if not drawn:
        plt.close(fig)
        return None
    ax.set_title("Margins (%)", fontsize=8, loc="left")
    ax.legend(fontsize=6, frameon=False)
    _style(ax)
    return _png(fig)


def fair_value_chart(values: dict, currency_symbol: str) -> bytes | None:
    low, base, high, price = (values.get(k) for k in ("low", "base", "high", "price"))
    if base is None or price is None:
        return None
    plt.rc("font", **FONT)
    fig, ax = plt.subplots(figsize=(6.8, 1.4))
    lo, hi = (low if low is not None else base), (high if high is not None else base)
    ax.axvspan(lo, hi, color=LIGHT, label="Fair value range (projection)")
    ax.axvline(base, color=ACCENT, linewidth=1.6, label=f"Base {currency_symbol}{base:,.0f}")
    ax.axvline(price, color="#b04a3a", linewidth=1.6, linestyle="--", label=f"Today {currency_symbol}{price:,.0f}")
    span = max(hi, price) - min(lo, price)
    ax.set_xlim(min(lo, price) - 0.15 * span, max(hi, price) + 0.15 * span)
    ax.set_yticks([])
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.set_title("Projection, not a reported figure: fair value a year out against today's price", fontsize=8,
                 loc="left")
    ax.legend(fontsize=6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=3)
    ax.tick_params(labelsize=7)
    return _png(fig)


def price_chart(dates: list[str], stock: list[float], benchmark: list[float | None] | None, currency_symbol: str,
                benchmark_name: str | None) -> bytes | None:
    """Weekly share price over up to five years, with the market index drawn from the same
    starting point (rebased to the stock's first price) so the two can be compared."""
    if len(stock) < 10:
        return None
    from datetime import date as _date
    x = [_date.fromisoformat(d) for d in dates]
    plt.rc("font", **FONT)
    fig, ax = plt.subplots(figsize=(6.8, 2.4))
    ax.plot(x, stock, color=ACCENT, linewidth=1.4, label="Share price")
    if benchmark and benchmark[0]:
        base = stock[0] / benchmark[0]
        ax.plot(x, [None if b is None else b * base for b in benchmark], color=MUTED, linewidth=1.0,
                linestyle="--", label=f"{benchmark_name or 'Market index'}, same starting point")
    # Kept short enough to fit the chart's width (the longer "about 5 years" version ran
    # past the right edge and lost its currency sign).
    ax.set_title(f"Share price, {x[0]:%b %Y} to {x[-1]:%d %b %Y} (weekly, adjusted for "
                 f"splits and dividends, {currency_symbol})", fontsize=8, loc="left")
    # The axis ends at the last price: no padding, so no tick for a year that has not
    # happened yet (the first version showed "2027" past the end of the line).
    ax.set_xlim(x[0], x[-1])
    ax.margins(x=0)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.legend(fontsize=6, frameon=False)
    _style(ax)
    return _png(fig)
