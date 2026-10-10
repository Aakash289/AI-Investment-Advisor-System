"""
verification/completeness.py: how much of the data the analysis needs actually came back (design 14.9).

What this file does:
    Fields are in three tiers (completeness_fields.yaml):

      critical     current price, shares, at least 3 years of revenue and net income,
                   equity, the user's budget. Any one missing withholds the amount,
                   whatever the score. The verdict is still given.
      important    weight 2: six years of history, margins, free cash flow, debt,
                   volatility, home-country macro, sector
      supporting   weight 1: news, geographic split, peer medians, other panel countries

    Score = weighted share of the important and supporting fields that came back.
      85% or more     proceed normally
      70% to 85%      confidence lowered one step
      below 70%       amount withheld, with the reason

    Two kinds of gap are left out of the score and listed separately, so a company is
    not marked down for them: gaps that exist by design (India's bond yield and credit
    spreads, analyst revisions), and fields whose agent is not built yet in this build
    (macro, news, geography, peer medians). Bank mode drops free cash flow and debt.
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.verification.record_index import RecordIndex


def _present(field: str, rule: dict, index: RecordIndex, extra: dict) -> bool:
    """Whether one field counts as present. extra holds facts that are not records:
    the budget, the sector label, and later the macro, news and peer results."""
    if field in extra:
        return bool(extra[field])
    if field == "six_years_history":
        return len(set(index.years("revenue"))) >= 6
    if field == "total_debt":
        return index.has("total_debt") or index.has("long_term_debt") or index.has("short_term_debt")
    if not index.has(field):
        return False
    min_years = rule.get("min_years")
    return len(set(index.years(field))) >= min_years if min_years else True


def score(records: list[dict], *, market: str | None, bank_mode: bool, extra: dict) -> dict:
    """Returns the score, the gate, the decision and every field's status."""
    cfg = load_yaml("verification/completeness_fields.yaml")
    index = RecordIndex(records)
    pending = {str(k): str(v) for k, v in (cfg.get("pending_until_built") or {}).items()}

    missing_critical = [f for f, rule in cfg["critical"].items() if not _present(f, rule or {}, index, extra)]

    weighted: dict[str, dict] = {}
    for tier in ("important", "supporting"):
        for field, rule in cfg[tier].items():
            if bank_mode and field in cfg["bank_mode_drop"]:
                continue
            if field in pending:
                continue
            weighted[field] = {"tier": tier, "weight": cfg["weights"][tier],
                               "present": _present(field, rule or {}, index, extra)}

    total = sum(w["weight"] for w in weighted.values())
    got = sum(w["weight"] for w in weighted.values() if w["present"])
    value = got / total if total else 1.0

    t = cfg["thresholds"]
    if missing_critical:
        decision, reason = "withhold_amount", "critical data missing: " + ", ".join(missing_critical)
    elif value < t["lower_confidence"]:
        decision, reason = "withhold_amount", f"only {value:.0%} of the data needed came back"
    elif value < t["proceed"]:
        decision, reason = "lower_confidence", f"{value:.0%} of the data needed came back"
    else:
        decision, reason = "proceed", None

    return {
        "score": round(value, 4),
        "decision": decision,
        "reason": reason,
        "missing_critical": missing_critical,
        "fields": weighted,
        "missing": sorted(f for f, w in weighted.items() if not w["present"]),
        "by_design_gaps": list(cfg["by_design_gaps"].get(market or "", [])),
        "pending_until_built": pending,
    }
