"""
analysis/fair_value.py: what the shares would be worth a year from now (design 14.10).

What this file does:
    Takes the year-1 projection and values it at the multiples similar companies trade
    at today (the peer medians from verification):

      method      calculation                                               used when
      forward P/E year-1 EPS x peer median P/E                              projected earnings positive
      EV/EBITDA   (year-1 EBITDA x peer median EV/EBITDA - net debt)/shares EBITDA positive; the only
                                                                            method for a loss-maker
      P/B         year-1 book value per share x peer median P/B             banks and insurers

    Where P/E and EV/EBITDA both apply, each case's fair value is the average of the two,
    and both figures are kept for the report. A fair value below zero (debt larger than
    the whole business is worth) is shown as zero.

    No fair value, with the reason, when:
      the company reports in a different currency from its trading currency (version 1
        does not cover the 20-F filers where this happens)
      there is no peer median for any method that applies; there is deliberately no
        fallback to the company's own multiple, which would only say "the price grows
        with earnings"
      both earnings and EBITDA are negative
      the projection itself could not be made
"""

from __future__ import annotations

from invest_agent.analysis.projection import CASES
from invest_agent.verification.record_index import RecordIndex

NAMES = {"pe": "forward P/E", "ev_ebitda": "EV/EBITDA", "pb": "P/B"}


def fair_value(proj: dict, peers: dict | None, records: list[dict], *, bank_mode: bool,
               currency_mismatch: bool = False) -> dict:
    """Returns {"status", "reason", "methods": {m: {case: value}}, "range": {case: value},
    "multiples": {m: median}, "price", "upside": {case: change}}."""
    out: dict = {"status": "ok", "methods": {}, "range": {}, "multiples": {}, "flags": []}
    if currency_mismatch:
        return {**out, "status": "none", "reason": "the company reports in a different currency from its shares"}
    if proj.get("status") != "ok":
        return {**out, "status": "none", "reason": "there is no projection (" + ", ".join(proj.get("flags", [])) + ")"}

    idx = RecordIndex(records)
    price = idx.value("current_price")
    shares = proj["assumptions"].get("shares")
    year1 = proj["years"][0]
    medians = {m: c["median"] for m, c in ((peers or {}).get("comparisons") or {}).items() if c.get("median")}
    net_debt = idx.value("net_debt", proj["base_year"])
    if net_debt is None and idx.value("total_debt", proj["base_year"]) is not None:
        net_debt = idx.value("total_debt", proj["base_year"]) - (idx.value("cash", proj["base_year"]) or 0)

    wanted = ["pb"] if bank_mode else ["pe", "ev_ebitda"]
    reasons = []
    for m in wanted:
        if m not in medians:
            reasons.append(f"no peer median for {NAMES[m]}")
            continue
        values = {}
        for case in CASES:
            row = proj["cases"][case][year1]
            if m == "pe" and row.get("eps") is not None and row["eps"] > 0:
                values[case] = row["eps"] * medians[m]
            elif m == "ev_ebitda" and row.get("ebitda") and row["ebitda"] > 0 and net_debt is not None and shares:
                values[case] = max(0.0, (row["ebitda"] * medians[m] - net_debt) / shares)
            elif m == "pb" and row.get("book_value_per_share") is not None and row["book_value_per_share"] > 0:
                values[case] = row["book_value_per_share"] * medians[m]
        if "base" in values:
            out["methods"][m] = values
            out["multiples"][m] = medians[m]
        else:
            reasons.append({"pe": "projected earnings are negative", "ev_ebitda": "projected EBITDA is negative "
                            "or net debt is unknown", "pb": "book value is zero or negative"}[m])

    if not out["methods"]:
        return {**out, "status": "none", "reason": "; ".join(reasons) or "no method applies"}

    for case in CASES:
        vals = [v[case] for v in out["methods"].values() if case in v]
        if vals:
            out["range"][case] = sum(vals) / len(vals)
    out["price"] = price
    out["year"] = year1
    if price:
        out["upside"] = {case: v / price - 1 for case, v in out["range"].items()}
    if reasons:
        out["flags"] += reasons
    return out
