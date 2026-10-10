"""
analysis/confidence.py: how sure the preliminary verdict is (design 14.11).

What this file does:
    Three levels, from how far the sub-scores agree:

      high     every sub-score points the same way as the verdict (for a neutral
               verdict: every sub-score stays within +-0.5)
      low      strong signals both ways: at least one sub-score at +1 or above and one
               at -1 or below (a great business at a stretched price, say)
      medium   anything else

    Then one step lower when completeness was 70% to 85% (verification), and one step
    lower when there is no valuation sub-score (scorecard version 3, 7 October: without it
    nothing checks the price, and the backtest replay found strong businesses scored
    "high confidence favourable" on business quality alone). Step 10 lowers it once more
    if the grounding guardrail removes a sentence.

    Calibration (checking how often past verdicts at each level held up, then adjusting)
    needs matured verdicts from the backtesting harness; until then the levels are
    uncalibrated and the result says so.
"""

from __future__ import annotations

from invest_agent.analysis.scorecard import config

LEVELS = ["low", "medium", "high"]
NAMES = {"business_quality": "business quality", "valuation": "valuation", "macro_fit": "economic fit",
         "news": "news", "forecast": "forecast"}


def _names(keys: list[str]) -> str:
    return ", ".join(NAMES.get(k, k) for k in keys)


def lower(level: str) -> str:
    return LEVELS[max(0, LEVELS.index(level) - 1)]


def assess(sub_scores: dict[str, float | None], verdict: str, *, completeness_decision: str | None) -> dict:
    """Returns {"level", "reasons": [text], "calibrated": False}."""
    cfg = config()["confidence"]
    present = {k: v for k, v in sub_scores.items() if v is not None}
    reasons = []
    strong_up = [k for k, v in present.items() if v >= cfg["strong"]]
    strong_down = [k for k, v in present.items() if v <= -cfg["strong"]]

    if strong_up and strong_down:
        level = "low"
        reasons.append(f"strong signals both ways: {_names(strong_up)} up, {_names(strong_down)} down")
    else:
        if verdict == "favourable":
            against = [k for k, v in present.items() if v <= -cfg["leaning"]]
        elif verdict == "unfavourable":
            against = [k for k, v in present.items() if v >= cfg["leaning"]]
        else:
            against = [k for k, v in present.items() if abs(v) > cfg["leaning"]]
        if against:
            level = "medium"
            reasons.append(f"not every sub-score agrees ({_names(against)} "
                           f"{'points' if len(against) == 1 else 'point'} the other way)")
        else:
            level = "high"
            reasons.append("every sub-score points the same way")

    # Too few sub-scores to judge agreement (7 October, spread check: Pfizer came out "high"
    # on 2 of 5 sub-scores, both 0): high needs at least min_scores_for_high of the five,
    # and with fewer than min_scores_for_medium the level is low whatever they say.
    n = len(present)
    if n < cfg.get("min_scores_for_medium", 0):
        if level != "low":
            reasons.append(f"lowered to low: only {n} of the 5 sub-scores could be worked out")
        level = "low"
    elif n < cfg.get("min_scores_for_high", 0) and level == "high":
        level = "medium"
        reasons.append(f"lowered to medium: only {n} of the 5 sub-scores could be worked out")

    if completeness_decision == "lower_confidence":
        level = lower(level)
        reasons.append("lowered one step: only part of the data came back (70% to 85%)")
    # Version 3 (7 October 2026, from the backtest replay): with no valuation against
    # similar companies, nothing checks the price paid, so a strong business alone looked
    # "high confidence favourable". Confidence drops one step when valuation is missing.
    if cfg.get("missing_valuation_lowers") and "valuation" in sub_scores and sub_scores["valuation"] is None:
        level = lower(level)
        reasons.append("lowered one step: there is no valuation against similar companies, so the price paid "
                       "is not checked")
    return {"level": level, "reasons": reasons, "calibrated": False}
