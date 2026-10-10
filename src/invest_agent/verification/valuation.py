"""
verification/valuation.py: the valuation step E5, the company's own multiples (design 14.9).

What this file does:
    From the verified price and latest annual figures it works out how the market
    values the company:

      market value     price x shares outstanding
      P/E              market value / net income            (not meaningful if a loss)
      P/B              market value / shareholders' equity  (not meaningful if equity <= 0)
      EV/EBITDA        (market value + debt - cash) / EBITDA (not meaningful if EBITDA <= 0;
                       never used for banks and insurers)
      P/S              market value / revenue
      dividend yield   dividends paid / market value (US), dividend per share / price (India)

    Market value / net income equals price / EPS, but uses the two figures already
    checked, so it cannot pick up a mismatched EPS figure.

    A loss-making company gets "not meaningful" for P/E, never a negative number. Each
    multiple becomes a record of source "computed" that lists the records it came from,
    so the grounding guardrail can trace it.

    If the company reports in a different currency from the one it trades in, the
    multiples would divide dollars by rupees, so they are not worked out here and the
    reason is given (version 1 does not cover the 20-F filers where this usually
    happens; convert_figures in fx/convert.py is ready for when it does).

    Peer medians (how the same multiples look for similar companies) are worked out in
    verification/peers.py.
"""

from __future__ import annotations

from invest_agent.common.records import make_record
from invest_agent.verification.record_index import RecordIndex


def _ids(index: RecordIndex, *fields: str, year: int | None = None) -> str:
    ids = []
    for f in fields:
        rows = index.by_field.get(f, [])
        row = next((r for r in rows if year is None or r.get("fiscal_year") in (year, None)), None)
        if row:
            ids.append(row["record_id"])
    return ",".join(ids)


def own_multiples(records: list[dict], *, ticker: str, bank_mode: bool, listing_currency: str | None,
                  reporting_currency: str | None) -> dict:
    """Returns {"multiples": {name: value or None}, "not_meaningful": {name: reason},
    "records": [computed records], "year", "currency", "status"}."""
    index = RecordIndex(records)
    out = {"multiples": {}, "not_meaningful": {}, "records": [], "year": index.latest_year(),
           "currency": listing_currency, "status": "ok"}

    if reporting_currency and listing_currency and reporting_currency != listing_currency:
        out["status"] = "currency_mismatch"
        out["note"] = f"Reports in {reporting_currency} but trades in {listing_currency}; multiples not worked out."
        return out

    year = out["year"]
    price_rec = index.latest("current_price")
    shares = index.value("shares_outstanding")
    if not price_rec or not shares or year is None:
        out["status"] = "missing_inputs"
        return out
    price, as_of = price_rec["value"], price_rec.get("as_of")
    mv = price * shares

    def put(name: str, value: float | None, inputs: str, reason_if_none: str | None = None) -> None:
        if value is None:
            if reason_if_none:
                out["not_meaningful"][name] = reason_if_none
            return
        out["multiples"][name] = value
        out["records"].append(make_record(
            source="computed", tier=2, entity=ticker, field=name, value=round(value, 6), as_of=as_of,
            period=f"FY{year}", fiscal_year=year, source_ref=inputs,
            unit=listing_currency if name == "market_value" else "ratio"))

    put("market_value", mv, _ids(index, "current_price", "shares_outstanding"))

    net = index.value("net_income", year)
    if net is not None:
        put("pe", mv / net if net > 0 else None, _ids(index, "current_price", "shares_outstanding", "net_income", year=year),
            "loss-making in the latest year")

    equity = index.value("total_equity", year)
    if equity is not None:
        put("pb", mv / equity if equity > 0 else None, _ids(index, "current_price", "shares_outstanding", "total_equity", year=year),
            "shareholders' equity is zero or negative")

    revenue = index.value("revenue", year)
    if revenue:
        put("ps", mv / revenue, _ids(index, "current_price", "shares_outstanding", "revenue", year=year))

    if not bank_mode:
        ebitda = index.value("ebitda", year)
        debt = index.value("total_debt", year)
        cash = index.value("cash", year)
        if ebitda is not None and debt is not None and cash is not None:
            ev = mv + debt - cash
            put("ev_ebitda", ev / ebitda if ebitda > 0 else None,
                _ids(index, "current_price", "shares_outstanding", "total_debt", "cash", "ebitda", year=year),
                "EBITDA is zero or negative")

    dividends = index.value("dividends_paid", year)
    dps = index.value("dividend_per_share_1y")
    if dividends is not None:
        put("dividend_yield", abs(dividends) / mv, _ids(index, "dividends_paid", "current_price", "shares_outstanding", year=year))
    elif dps is not None:
        put("dividend_yield", dps / price, _ids(index, "dividend_per_share_1y", "current_price"))
    return out
