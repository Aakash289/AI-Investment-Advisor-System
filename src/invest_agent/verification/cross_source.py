"""
verification/cross_source.py: what happens when two sources give the same figure (design 14.9).

What this file does:
    In the normal case each figure has one source (SEC or Trendlyne for statements,
    Yahoo for prices), so conflicts are rare. They can still happen, for example when a
    fallback source filled part of the data. For every figure that more than one source
    gave for the same period:

      1. measure the gap: |a - b| / the larger of the two
      2. within the tolerance for that kind of figure (0.5% statements, 1% prices):
         no conflict, keep the higher-tier value
      3. a ratio close to 1,000, 1,000,000 or 1,00,00,000 (crore): a units mistake in one
         of them (thousands or crore against units). The higher-tier value is kept,
         flagged, and logged as a units mistake rather than a real disagreement
      4. otherwise a real conflict: the higher-tier value wins (on a tie, the first
         source given), and both are written to the conflict log with the reason

    Nothing is re-queried: the official value has already been fetched.
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml


def _gap(a: float, b: float) -> float:
    biggest = max(abs(a), abs(b))
    return 0.0 if biggest == 0 else abs(a - b) / biggest


def units_factor(a: float, b: float, factors: list[float], closeness: float) -> float | None:
    """The factor by which a is larger than b, if the two differ by a units mistake."""
    if a == 0 or b == 0:
        return None
    ratio = abs(a / b)
    for f in factors:
        for candidate in (f, 1 / f):
            if abs(ratio / candidate - 1) <= closeness:
                return candidate
    return None


def _kind(field: str, cfg: dict) -> str:
    return "price" if field in cfg["kinds"]["price"] else "statement"


def resolve(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (records with one value per figure and period, conflict log entries)."""
    cfg = load_yaml("verification/tolerances.yaml")
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        key = (r.get("entity"), r["field"], r.get("fiscal_year") or r.get("period_end") or "latest")
        groups.setdefault(key, []).append(r)

    kept, conflicts = [], []
    for (_, field, period), rows in groups.items():
        numeric = [r for r in rows if isinstance(r.get("value"), (int, float)) and not isinstance(r.get("value"), bool)]
        if len({r["source"] for r in numeric}) < 2:
            kept += rows                                  # one source: nothing to compare
            continue

        # Best first: lower tier number is more trusted (1 official, 2 aggregator).
        ordered = sorted(numeric, key=lambda r: r.get("tier", 9))
        winner, other = dict(ordered[0]), ordered[1]
        tolerance = cfg["conflict"][_kind(field, cfg)]
        gap = _gap(winner["value"], other["value"])
        if gap <= tolerance:
            kept.append(winner)
            continue

        entry = {"field": field, "period": period,
                 "values": [{"source": r["source"], "tier": r.get("tier"), "value": r["value"]} for r in ordered[:2]],
                 "tolerance": tolerance, "gap": round(gap, 6)}
        factor = units_factor(other["value"], winner["value"], cfg["units_mistake"]["factors"],
                              cfg["units_mistake"]["closeness"])
        if factor is not None:
            entry.update({"resolution": "units_corrected", "kept": winner["source"],
                          "reason": f"{other['source']} differs by a factor of {factor:g}, a units mistake"})
            winner["flags"] = sorted(set(winner.get("flags", [])) | {"units_mistake_resolved"})
        else:
            entry.update({"resolution": "higher_tier_wins", "kept": winner["source"],
                          "reason": f"tier {winner.get('tier')} beats tier {other.get('tier')}"
                          if winner.get("tier") != other.get("tier") else "same tier: first source kept"})
            winner["flags"] = sorted(set(winner.get("flags", [])) | {"conflict_resolved"})
        kept.append(winner)
        conflicts.append(entry)
    return kept, conflicts
