"""
analysis/forecasting.py: F4, the Forecasting step (design 14.10).

What this file does:
    Runs the projection (projection.py) and the fair value (fair_value.py), then tags
    every projected number so it can never be mistaken for a reported one:

      each year-1 and year-2 figure (revenue, EPS, EBITDA, book value per share) and each
      fair value (low, base, high) becomes a record of source "projected", flagged
      "projected", with the case, the method and the assumptions in plain words

    The Grounding Guardrail (step 10) only lets Synthesis quote a projected number
    through one of these records, and the report shows them in their own section.

    The year-1 records are what the backtesting harness (step 15) compares with the next
    annual report; storing them in the decision log comes with step 14.

    Plain code, no model call and no outside call.
"""

from __future__ import annotations

from invest_agent.analysis.fair_value import NAMES, fair_value
from invest_agent.analysis.projection import CASES, project
from invest_agent.common.records import make_record

FIELDS = ("revenue", "eps", "ebitda", "book_value_per_share")


def _assumption_text(proj: dict, case: str) -> str:
    a = proj["assumptions"]
    ebitda = f", EBITDA margin {a['ebitda_margin'][case] * 100:.1f}%" if a.get("ebitda_margin") else ""
    return (f"{case} case: revenue growth {a['growth'][case] * 100:.1f}% a year (trend {a['trend_growth'] * 100:.1f}%), "
            f"net margin {a['net_margin'][case] * 100:.1f}%{ebitda}, shares held at today's count")


def tag_records(proj: dict, fv: dict, *, ticker: str, as_of: str | None) -> list[dict]:
    records = []
    if proj.get("status") != "ok":
        return records
    for case in CASES:
        for year, row in proj["cases"][case].items():
            for field in FIELDS:
                if row.get(field) is None:
                    continue
                records.append(make_record(
                    source="projected", tier=3, entity=ticker, field=f"{field}_{case}", value=round(row[field], 6),
                    as_of=as_of, period=f"FY{year}", fiscal_year=year, flags=["projected"],
                    source_ref=_assumption_text(proj, case)))
    if fv.get("status") == "ok":
        methods = ", ".join(f"{NAMES[m]} at the peer median of {v:.1f}" for m, v in fv["multiples"].items())
        for case, value in fv["range"].items():
            records.append(make_record(
                source="projected", tier=3, entity=ticker, field=f"fair_value_{case}", value=round(value, 6),
                as_of=as_of, period=f"FY{fv['year']}", fiscal_year=fv["year"], flags=["projected"],
                source_ref=f"{methods}; {_assumption_text(proj, case)}"))
    return records


def forecast(records: list[dict], valuation: dict | None, *, ticker: str, bank_mode: bool,
             unbroken_years: list[int] | None) -> dict:
    """Returns {"projection", "fair_value", "records"}."""
    proj = project(records, unbroken_years=unbroken_years)
    valuation = valuation or {}
    fv = fair_value(proj, valuation.get("peers"), records, bank_mode=bank_mode,
                    currency_mismatch=valuation.get("status") == "currency_mismatch")
    price_as_of = next((r.get("as_of") for r in records if r.get("field") == "current_price"), None)
    return {"projection": proj, "fair_value": fv, "records": tag_records(proj, fv, ticker=ticker, as_of=price_as_of)}
