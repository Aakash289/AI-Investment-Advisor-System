"""
verification/data_notes.py: turns the verification results into plain sentences (design 14.9).

What this file does:
    The decision log keeps every check in full. People need much less, so this file
    writes two things:

      notes        the report's "Data notes" section: anything excluded, stale or
                   recomputed, conflicts and how they were settled, gaps that exist by
                   design, and data this build does not fetch yet
      user_line    one line for the compact answer, only when data quality changed the
                   outcome: the amount withheld, confidence lowered, or the analysis
                   resting on Tier 2 data only (Indian fundamentals come from Trendlyne,
                   an aggregator, not the exchange)
"""

from __future__ import annotations

GAP_NAMES = {
    "india_bond_yield": "India's 10-year bond yield",
    "india_credit_spread": "Indian credit spreads",
    "analyst_revisions": "analyst estimate revisions",
    "home_macro": "home-country economy",
    "panel_macro": "other major economies",
    "news": "news and disclosures",
    "geographic_split": "revenue by country",
    "sector": "sector label",
    "peer_medians": "peer comparison",
}


def build_notes(findings: list[dict], conflicts: list[dict], completeness: dict) -> list[str]:
    notes = [f"{f['field'].replace('_', ' ').capitalize()}: {f['detail']} ({f['action']})." for f in findings]
    for c in conflicts:
        a, b = c["values"][0], c["values"][1]
        notes.append(f"{c['field'].replace('_', ' ').capitalize()} {c['period']}: {a['source']} gave {a['value']:,.4g} and "
                     f"{b['source']} gave {b['value']:,.4g}; kept {c['kept']} ({c['reason']}).")
    if completeness.get("missing"):
        notes.append("Not available: " + ", ".join(GAP_NAMES.get(f, f.replace("_", " ")) for f in completeness["missing"]) + ".")
    if completeness.get("by_design_gaps"):
        notes.append("Known gaps, by design: " + ", ".join(GAP_NAMES.get(g, g) for g in completeness["by_design_gaps"]) + ".")
    pending = completeness.get("pending_until_built") or {}
    if pending:
        notes.append("Not fetched yet in this build: " + ", ".join(GAP_NAMES.get(p, p) for p in pending) + ".")
    return notes


def user_line(completeness: dict, tier2_only: bool) -> str | None:
    decision = completeness.get("decision")
    if decision == "withhold_amount":
        return f"No amount is given because {completeness['reason']}."
    if decision == "lower_confidence":
        return f"Confidence is lowered one step because only {completeness['reason']}."
    if tier2_only:
        return "The company's figures come from an aggregator (Trendlyne), not directly from the exchange."
    return None
