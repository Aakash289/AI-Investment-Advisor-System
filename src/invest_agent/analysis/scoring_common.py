"""
analysis/scoring_common.py: the shared mechanics of every scorer (design 14.18).

What this file does:
    Every sub-score in the analysis is built the same way, so it can always be explained
    in one sentence ("ROIC of 18% scores +2 because anything above 15% does"):

      band()          looks a value up in a band list from scoring_bands.yaml and returns a
                      whole-step score from -2 to +2
      combine()       adds up component scores by their weights. A missing component's
                      weight is shared out among the others; if more than half the weight
                      is missing, the sub-score itself is missing (None), never guessed
      finding()       one plain-language result with its display value and the record IDs
                      behind it, so Synthesis can cite it and the grounding guardrail can
                      match the number exactly
      pct(), times()  the display formats findings use, worked out once here so every
                      finding shows a number the same way
"""

from __future__ import annotations


def band(value: float, bands: list) -> int:
    """bands: [[threshold, score], ...] read from the top; a None threshold catches the rest."""
    for threshold, score in bands:
        if threshold is None or value >= threshold:
            return int(score)
    return int(bands[-1][1])


def smooth_band(value: float, bands: list) -> float:
    """The same bands without the jumps (scorecard version 6, 7 to 10 October: Microsoft's
    P/E sat 20% above its peers, exactly on a band edge, so a small price move flipped its
    valuation between 0 and -1 and its verdict between Neutral and Favourable).

    Each band's score is reached in the middle of the band and the score slides in a
    straight line between neighbouring middles, so at an edge it is halfway between the
    two bands, or about halfway when the bands differ in width (20% dearer gives -0.5 instead of 0 or -1). The open-ended bands at either
    end reach their score at their edge and stay there. On average a stock scores what the
    step bands gave; it just never jumps.

    bands: the same [[threshold, score], ...] list as band(), thresholds from the top."""
    knots: list[tuple[float, float]] = []
    thresholds = [t for t, _ in bands]
    for i, (t, s) in enumerate(bands):
        if i == 0:
            knots.append((float(t), float(s)))                       # top open band: from its edge
        elif t is None:
            knots.append((float(thresholds[i - 1]), float(s)))       # bottom open band: from its edge
        else:
            knots.append(((float(thresholds[i - 1]) + float(t)) / 2, float(s)))   # middle of the band
    # knots run from the highest value down
    if value >= knots[0][0]:
        return knots[0][1]
    if value <= knots[-1][0]:
        return knots[-1][1]
    for (x_hi, s_hi), (x_lo, s_lo) in zip(knots, knots[1:]):
        if x_lo <= value <= x_hi:
            if x_hi == x_lo:
                return s_hi
            return round(s_lo + (s_hi - s_lo) * (value - x_lo) / (x_hi - x_lo), 2)
    return knots[-1][1]


def combine(scores: dict[str, int | None], weights: dict[str, float], missing_limit: float = 0.5) -> dict:
    """Weighted score with missing components reweighted. Returns {score, used_weight, missing}."""
    present = {k: v for k, v in scores.items() if v is not None and k in weights}
    total = sum(weights.values())
    used = sum(weights[k] for k in present)
    missing = sorted(k for k in weights if k not in present)
    if total == 0 or used / total < 1 - missing_limit:
        return {"score": None, "used_weight": round(used / total, 4) if total else 0, "missing": missing}
    value = sum(weights[k] * v for k, v in present.items()) / used
    return {"score": round(value, 4), "used_weight": round(used / total, 4), "missing": missing}


def finding(component: str, score: int | None, text: str, display: dict, record_ids: list[str]) -> dict:
    return {"component": component, "score": score, "text": text, "display": display,
            "record_ids": [r for r in record_ids if r]}


def pct(value: float, decimals: int = 1, signed: bool = False) -> str:
    return f"{value * 100:+.{decimals}f}%" if signed else f"{value * 100:.{decimals}f}%"


def times(value: float) -> str:
    return f"{value:.1f}x"
