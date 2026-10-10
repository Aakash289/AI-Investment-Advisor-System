"""
mcp_servers/filings/normaliser.py: turns SEC's company facts file into six clean years per figure.

What this file does:
    SEC's company facts file lists every value a company has ever reported for every
    XBRL tag: annual and quarterly, original and restated, this year's figure and the
    same figure repeated as last year's comparison in the next report. This file picks
    out one clean annual series per figure, following the rules in design 14.3:

      1. Full-year periods only: 10-K filings (or amended 10-K/A), and for figures over
         a period, a period of roughly a year (350 to 380 days).
      2. The fiscal year is taken from the period's end date, not from SEC's "fy" field,
         because a 10-K repeats last year's figures under this year's "fy".
      3. If the same year was reported more than once (a restatement, or a later
         comparison), the most recently filed value wins.
      4. The tag list in xbrl_tag_map.yaml is tried in order, year by year, so a company
         that switched tags (NVIDIA's revenue) still gets one continuous series.
      5. Six fiscal years are kept, for a 5-year growth rate. A year with no value stays
         missing: nothing is filled in.

    Plain code, no network calls, tested against saved files.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from invest_agent.common.yaml_loader import load_yaml

YEARS = 6
ANNUAL_FORMS = {"10-K", "10-K/A"}


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _annual_points(entries: list[dict], kind: str, as_of: str | None = None) -> dict[str, dict]:
    """From one tag's entries, the latest-filed annual value for each period end date.
    With as_of (a date), only what had been filed by then counts (the backtest replay)."""
    best: dict[str, dict] = {}
    for e in entries:
        if e.get("form") not in ANNUAL_FORMS or "end" not in e or e.get("val") is None:
            continue
        if as_of and (e.get("filed") or "9999") > as_of:
            continue                          # filed after the replay date: not known then
        if kind == "duration":
            if not e.get("start") or not 350 <= _days(e["start"], e["end"]) <= 380:
                continue                      # a quarter or a part year, not a full year
        elif e.get("start"):
            continue                          # balance items have only an end date
        current = best.get(e["end"])
        if current is None or e.get("filed", "") > current.get("filed", ""):
            best[e["end"]] = e                # the most recently filed value wins
    return best


def normalise_company_facts(facts: dict[str, Any], as_of: str | None = None) -> dict[str, list[dict]]:
    """Return {figure: [{fiscal_year, period_end, value, unit, tag, accession, filed}, ...]},
    newest year first, at most six years, for every figure in xbrl_tag_map.yaml.

    as_of ("2023-03-31") keeps only facts filed on or before that date, so the figures are
    the ones the company had reported by then, restatements filed later included only if
    they were filed by that date (step 15, the backtest replay)."""
    tag_map = load_yaml("mcp_servers/filings/xbrl_tag_map.yaml")
    all_facts = facts.get("facts", {})
    out: dict[str, list[dict]] = {}

    for figure, spec in tag_map.items():
        taxonomy = all_facts.get(spec.get("taxonomy", "us-gaap"), {})
        by_year: dict[int, dict] = {}
        for tag in spec["tags"]:                       # tags in order of preference
            # "us-gaap:Name" reads that one tag from another taxonomy than the figure's own.
            source = all_facts.get(tag.split(":", 1)[0], {}) if ":" in tag else taxonomy
            name = tag.split(":", 1)[1] if ":" in tag else tag
            entries = source.get(name, {}).get("units", {}).get(spec["unit"], [])
            for end, e in _annual_points(entries, spec["kind"], as_of).items():
                fiscal_year = int(end[:4])
                if fiscal_year not in by_year:         # an earlier tag already gave this year
                    by_year[fiscal_year] = {
                        "fiscal_year": fiscal_year, "period_end": end, "value": float(e["val"]),
                        "unit": spec["unit"], "tag": tag, "accession": e.get("accn"),
                        "filed": e.get("filed"),
                    }
        years = sorted(by_year, reverse=True)[:YEARS]
        if years:
            out[figure] = [by_year[y] for y in years]
    rebuild_operating_income(out)
    rebuild_depreciation(out)
    return out


def rebuild_depreciation(out: dict[str, list[dict]]) -> None:
    """Fill depreciation and amortisation for years that have no combined figure, as
    depreciation plus amortisation of intangible assets (Microsoft, 7 October: without it
    there was no EBITDA, so no EV/EBITDA, net debt to EBITDA or EBITDA-based fair value).
    Amortisation counts as 0 when it is not reported; the parts are then dropped, since
    nothing else uses them."""
    dep = {r["fiscal_year"]: r for r in out.pop("depreciation_only", [])}
    amort = {r["fiscal_year"]: r["value"] for r in out.pop("intangible_amortization", [])}
    if not dep:
        return
    have = {r["fiscal_year"] for r in out.get("depreciation_amortization", [])}
    added = [{**d, "value": d["value"] + amort.get(y, 0.0), "tag": "rebuilt:depreciation+intangible_amortization"}
             for y, d in dep.items() if y not in have]
    if added:
        rows = sorted(out.get("depreciation_amortization", []) + added, key=lambda r: r["fiscal_year"], reverse=True)
        out["depreciation_amortization"] = rows[:YEARS]


def rebuild_operating_income(out: dict[str, list[dict]]) -> None:
    """Fill operating income for years that have none, as pre-tax income plus interest
    expense (decided 7 October, spread check: Pfizer reports no operating income line, and
    Johnson & Johnson's latest year has none, so their profitability, margin trend and
    balance sheet could not be scored). The tag says it was rebuilt, so the report and the
    data notes can say so. Interest expense counts as 0 when it is not reported."""
    pretax = {r["fiscal_year"]: r for r in out.get("pretax_income", [])}
    if not pretax:
        return
    have = {r["fiscal_year"] for r in out.get("operating_income", [])}
    interest = {r["fiscal_year"]: r["value"] for r in out.get("interest_expense", [])}
    added = [{**p, "value": p["value"] + interest.get(y, 0.0),
              "tag": "rebuilt:pretax_income+interest_expense"}
             for y, p in pretax.items() if y not in have]
    if added:
        rows = sorted(out.get("operating_income", []) + added, key=lambda r: r["fiscal_year"], reverse=True)
        out["operating_income"] = rows[:YEARS]
