"""
verification/sanity.py: the sanity checks every gathered figure goes through (design 14.9).

What this file does:
    Five checks, all plain code:

      range         a gross margin above 100%, shares or a price at or below zero, or
                    negative debt cannot be right: the figure is excluded and flagged
      EPS           reported EPS should be within 5% of net income / shares; if not, it
                    is flagged and a recomputed EPS is added beside it
      currency      the price's currency should match the listing; figures reported in
                    another currency are flagged for the valuation step (the ADR case)
      freshness     the latest annual figures should be under 18 months old and the
                    price under 5 business days old; much older counts as missing
      continuity    the fiscal years held should run without a gap; growth rates are only
                    valid over unbroken stretches

    check_records() returns the records that pass, each with any flags it earned, plus
    a list of findings (one dict per problem) for the decision log and the data notes.
"""

from __future__ import annotations

from datetime import date

from invest_agent.common.records import make_record
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.fx.checks import business_days_between, to_date
from invest_agent.verification.record_index import RecordIndex

# Figures that must be positive, and figures that must not be negative.
MUST_BE_POSITIVE = {"shares_outstanding", "current_price"}
NOT_NEGATIVE = {"long_term_debt", "short_term_debt", "total_debt"}


def _finding(check: str, field: str, detail: str, action: str, record_id: str | None = None) -> dict:
    return {"check": check, "field": field, "detail": detail, "action": action, "record_id": record_id}


def _months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.44                 # average month length in days


def range_check(records: list[dict]) -> tuple[list[dict], list[dict]]:
    kept, findings = [], []
    for r in records:
        v = r.get("value")
        bad = None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            kept.append(r)
            continue
        if r["field"] == "gross_margin" and v > 1:
            bad = f"gross margin of {v:.0%} is above 100%"
        elif r["field"] in MUST_BE_POSITIVE and v <= 0:
            bad = f"{r['field']} is {v}, not above zero"
        elif r["field"] in NOT_NEGATIVE and v < 0:
            bad = f"{r['field']} is negative ({v})"
        if bad:
            findings.append(_finding("range", r["field"], bad, "excluded", r["record_id"]))
        else:
            kept.append(r)
    return kept, findings


def eps_check(index: RecordIndex, ticker: str, tolerance: float) -> tuple[list[dict], list[dict]]:
    """Compare reported EPS (diluted for US, basic for India) with net income / shares,
    for the latest year. Shares are today's count, so a company buying back shares can
    differ by a little; 5% allows for that."""
    eps_field = "eps_diluted" if index.has("eps_diluted") else "eps_basic"
    year = index.latest_year()
    reported = index.value(eps_field, year) if year else None
    net, shares = index.value("net_income", year) if year else None, index.value("shares_outstanding")
    if reported is None or net is None or not shares:
        return [], []
    recomputed = net / shares
    if reported == 0 or abs(reported - recomputed) / abs(recomputed) <= tolerance:
        return [], []
    record = make_record(source="computed", tier=2, entity=ticker, field="eps_recomputed", value=round(recomputed, 4),
                         as_of=str(year), period=f"FY{year}", fiscal_year=year,
                         source_ref=f"net_income FY{year} / shares_outstanding", flags=["eps_mismatch"])
    detail = f"reported {eps_field} {reported:.2f} against {recomputed:.2f} from net income / shares"
    return [record], [_finding("eps", eps_field, detail, "flagged; EPS recomputed from its parts")]


def currency_check(index: RecordIndex, listing_currency: str | None, reporting_currency: str | None) -> list[dict]:
    findings = []
    price = index.latest("current_price")
    if price and listing_currency and price.get("unit") not in (None, listing_currency):
        findings.append(_finding("currency", "current_price",
                                 f"price in {price.get('unit')} but the listing is in {listing_currency}", "flagged"))
    if reporting_currency and listing_currency and reporting_currency != listing_currency:
        findings.append(_finding("currency", "fundamentals",
                                 f"reports in {reporting_currency}, trades in {listing_currency}",
                                 "valuation converts per-share figures (ADR case)"))
    return findings


def freshness_check(records: list[dict], index: RecordIndex, today: date, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Returns (records to drop because they are very stale, findings)."""
    drop, findings = [], []
    year = index.latest_year()
    if year:
        latest_revenue = index.latest("revenue")
        end = latest_revenue.get("period_end") or f"{year}-12-31"
        months = _months_between(to_date(end), today)
        if months > cfg["fiscal_year_missing_months"]:
            drop += [r for r in records if r.get("fiscal_year") == year]
            findings.append(_finding("freshness", "fundamentals", f"latest annual figures end {end}, {months:.0f} months ago",
                                     "counted as missing"))
        elif months > cfg["fiscal_year_stale_months"]:
            findings.append(_finding("freshness", "fundamentals", f"latest annual figures end {end}, {months:.0f} months ago",
                                     "flagged stale"))
    price = index.latest("current_price")
    if price and price.get("as_of"):
        days = business_days_between(price["as_of"], today)
        if days > cfg["price_missing_business_days"]:
            drop.append(price)
            findings.append(_finding("freshness", "current_price", f"price is from {price['as_of']}, {days} business days ago",
                                     "counted as missing", price["record_id"]))
        elif days > cfg["price_stale_business_days"]:
            findings.append(_finding("freshness", "current_price", f"price is from {price['as_of']}, {days} business days ago",
                                     "flagged stale", price["record_id"]))
    return drop, findings


def continuity_check(index: RecordIndex) -> tuple[list[int], list[dict]]:
    """The unbroken run of fiscal years from the latest one back, and a finding per gap."""
    years = sorted(set(index.years("revenue")), reverse=True)
    if not years:
        return [], []
    run = [years[0]]
    for y in years[1:]:
        if y != run[-1] - 1:
            break
        run.append(y)
    missing = [y for y in range(years[-1], years[0]) if y not in years]
    findings = [_finding("continuity", "revenue", f"no figures for FY{y}", "growth uses the unbroken years only")
                for y in missing]
    return run, findings


def check_records(records: list[dict], *, ticker: str, listing_currency: str | None,
                  reporting_currency: str | None, today: date | None = None) -> dict:
    """Run every check. Returns {records, findings, unbroken_years}."""
    cfg = load_yaml("verification/tolerances.yaml")["sanity"]
    today = today or date.today()

    kept, findings = range_check(records)
    index = RecordIndex(kept)

    added, eps_findings = eps_check(index, ticker, cfg["eps_tolerance"])
    findings += eps_findings + currency_check(index, listing_currency, reporting_currency)

    drop, fresh_findings = freshness_check(kept, index, today, cfg)
    findings += fresh_findings
    drop_ids = {r["record_id"] for r in drop}
    kept = [r for r in kept if r["record_id"] not in drop_ids] + added

    unbroken, gap_findings = continuity_check(RecordIndex(kept))
    findings += gap_findings
    return {"records": kept, "findings": findings, "unbroken_years": unbroken}
