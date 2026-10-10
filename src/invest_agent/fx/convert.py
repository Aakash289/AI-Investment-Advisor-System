"""
fx/convert.py: converts an amount from one currency to another, keeping the original.

What this file does:
    Design 14.8 converts money only where two currencies actually meet:

      1. position sizing    the user's budget into the stock's currency, and the amount
                            to invest back again (used now by the FX step, and by sizing
                            in step 11)
      2. valuation          EPS, book value per share and EBITDA into the trading
                            currency, only for a company that reports in one currency and
                            trades in another (used by valuation in step 7)
      3. what the user sees amounts shown in the user's currency (report, step 13)

    Ratios (margins, ROE, growth, volatility) have no currency and are never converted,
    and historical figures are never converted at today's rate, since that would invent
    growth or decline that never happened.

    Every conversion keeps the original amount and records the rate, its date and its
    source next to the result, so the report can say exactly how a number was produced.
    Amounts are stored at full precision; rounding happens only in format_money, for
    display: rupees to whole rupees, dollars to cents.
"""

from __future__ import annotations

from invest_agent.profile.budget_parser import format_amount

INDICATIVE_NOTE = ("Converted amounts are indicative: they use official mid-market reference rates, "
                   "and a bank will charge a spread on top.")
ABROAD_COSTS_NOTE = "Costs of investing abroad (remittance charges and tax collected at source) are not included."

SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}
NAMES = {"INR": "rupee", "USD": "dollar", "EUR": "euro", "GBP": "pound"}


def convert(amount: float, from_currency: str, to_currency: str, rate: dict) -> dict:
    """Convert one amount with a rate from fx_server (rate["rate"] is quote per base).

    Works in either direction: with USD to INR at 96.2, dollars are multiplied and
    rupees divided. Returns the original, the converted amount and the rate details.
    """
    if from_currency == to_currency:
        converted = amount
    elif (from_currency, to_currency) == (rate["base"], rate["quote"]):
        converted = amount * rate["rate"]
    elif (from_currency, to_currency) == (rate["quote"], rate["base"]):
        converted = amount / rate["rate"]
    else:
        raise ValueError(f"A {rate['base']}/{rate['quote']} rate cannot convert {from_currency} to {to_currency}")
    return {
        "original": {"amount": amount, "currency": from_currency},
        "converted": {"amount": converted, "currency": to_currency},
        "rate": rate["rate"], "rate_pair": f"{rate['base']}/{rate['quote']}",
        "rate_date": rate.get("date"), "source": rate.get("source"), "provider": rate.get("provider"),
        "indicative": from_currency != to_currency,
    }


def convert_figures(values: dict[str, float], from_currency: str, to_currency: str, rate: dict) -> dict[str, dict]:
    """Conversion point 2: several per-share or company figures at once (ADR valuation)."""
    return {name: convert(v, from_currency, to_currency, rate) for name, v in values.items() if v is not None}


def format_money(amount: float, currency: str) -> str:
    """Display rounding only: whole rupees with Indian grouping (₹2,00,000), dollars and
    other currencies to cents ($2,079.00). A negative amount puts the minus sign first
    (-₹1,119, not ₹-1,119)."""
    if amount is not None and amount < 0:
        return "-" + format_money(-amount, currency)
    if currency == "INR":
        return format_amount(amount, "INR")
    symbol = SYMBOLS.get(currency)
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def format_large(amount: float, currency: str) -> str:
    """Company-sized amounts the way people read them: rupees in crore with Indian
    grouping (₹10,85,997 crore), dollars in billions or trillions ($416.2 billion). A
    negative amount puts the minus sign first (-₹1,119 crore)."""
    if amount is not None and amount < 0:
        return "-" + format_large(-amount, currency)
    if currency == "INR":
        return f"{format_amount(amount / 1e7, 'INR')} crore"
    symbol = SYMBOLS.get(currency, "")
    if abs(amount) >= 1e12:
        return f"{symbol}{amount / 1e12:,.2f} trillion"
    if abs(amount) >= 1e9:
        return f"{symbol}{amount / 1e9:,.1f} billion"
    return format_money(amount, currency)


def describe_rate(rate: dict) -> str:
    """The rate in words for the user, e.g. 1 USD = 96.20 INR (Frankfurter, FBIL, 2026-10-05)."""
    names = {"frankfurter": "Frankfurter", "fred": "FRED", "yfinance": "Yahoo Finance"}
    source = names.get(rate.get("source"), rate.get("source"))
    return f"1 {rate['base']} = {rate['rate']:,.2f} {rate['quote']} ({source}, {rate.get('provider')}, {rate.get('date')})"
