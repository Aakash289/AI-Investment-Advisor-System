"""
fx/risk_note.py: the one-line currency risk note for cross-border investments.

What this file does:
    When the stock trades in a different currency from the user's budget, the user
    carries currency risk as well as the stock's own risk. An Indian investor holding a
    US stock gains when the dollar rises against the rupee and loses when it falls
    (design 14.8). This file writes one plain sentence on how the stock's currency moved
    against the user's over the past 12 months, from Frankfurter's monthly series and
    today's rate. It is context, not a forecast, and says so.
"""

from __future__ import annotations

from invest_agent.fx.convert import NAMES, SYMBOLS

FLAT_BELOW = 0.005          # moves under 0.5% are described as barely moving


def risk_note(points: list[dict], rate: dict, user_currency: str, stock_currency: str) -> str | None:
    """points: [{date, rate}] oldest first; rate: today's rate from fx_server.
    Both must be quoted as user currency per one unit of stock currency (rupees per
    dollar). Returns None if there is not enough to say anything."""
    if not points or rate.get("rate") is None:
        return None
    if (rate["base"], rate["quote"]) != (stock_currency, user_currency):
        raise ValueError("The risk note needs the rate as user currency per unit of stock currency")

    start, end = points[0]["rate"], rate["rate"]
    change = end / start - 1
    stock_name = NAMES.get(stock_currency, stock_currency)
    user_name = NAMES.get(user_currency, user_currency)
    symbol = SYMBOLS.get(user_currency, "")
    levels = f"{symbol}{start:,.2f} to {symbol}{end:,.2f} per {stock_name}"

    if abs(change) < FLAT_BELOW:
        moved = f"Over the past 12 months the {stock_name} barely moved against the {user_name} ({levels})."
    else:
        direction = "rose" if change > 0 else "fell"
        moved = f"Over the past 12 months the {stock_name} {direction} {abs(change) * 100:.1f}% against the {user_name} ({levels})."
    effect = (f"For a {user_name} investor in a {stock_name} stock, a rising {stock_name} adds to returns "
              f"and a falling one takes away from them. This is context, not a forecast.")
    return f"{moved} {effect}"
