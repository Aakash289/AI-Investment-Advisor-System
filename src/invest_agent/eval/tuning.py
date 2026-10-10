"""
eval/tuning.py: would other weights or thresholds have done better? (design 14.17)

What this file does:
    Given scored verdicts that keep their sub-scores (the replay keeps them), it works
    out what the verdicts would have been under other settings and compares:

      thresholds   the favourable and unfavourable lines at +-0.25, 0.5, 0.75 (today)
                   and 1.0
      weights      today's (50 / 20 / 15 / 5 / 10) and a few others set out below

    The measure is the separation: how much better the favourable verdicts did than all
    the others, on average, against the market over the next 12 months. A setting that
    calls almost nothing favourable cannot win by luck: it needs at least 5 favourable and
    5 other verdicts to count.

    Safeguards from the design (14.17):
      - nothing before 100 scored verdicts
      - the settings are chosen on 70% of the verdicts and checked on the other 30%; a
        change is suggested only if, on the 30% it never saw, its favourable picks beat
        the rest (above 0) and it does better than today's by at least 2 points
      - the split is fixed (by company and date), so running it twice gives the same answer
      - it only suggests: a change is made by hand in scorecard_config.yaml, after Aakash
        approves it, with a new version number
"""

from __future__ import annotations

import hashlib
from itertools import product
from statistics import mean

from invest_agent.analysis.scorecard import config as scorecard_config
from invest_agent.eval.scoring import config

THRESHOLDS = [0.25, 0.5, 0.75, 1.0]
WEIGHT_SETS = {
    "current": None,                                   # scorecard_config.yaml as it is
    "quality heavier": {"business_quality": 60, "valuation": 15, "macro_fit": 10, "news": 5, "forecast": 10},
    "valuation heavier": {"business_quality": 40, "valuation": 30, "macro_fit": 15, "news": 5, "forecast": 10},
    "forecast heavier": {"business_quality": 45, "valuation": 20, "macro_fit": 10, "news": 5, "forecast": 20},
    "equal": {"business_quality": 20, "valuation": 20, "macro_fit": 20, "news": 20, "forecast": 20},
}
MIN_GROUP = 5
MIN_GAIN = 0.02


def weighted(sub_scores: dict, weights: dict, low_reliability: list[str] | None, factor: float) -> float | None:
    w = {k: v * (factor if k in (low_reliability or []) else 1.0) for k, v in weights.items()}
    present = {k: v for k, v in (sub_scores or {}).items() if v is not None and k in w}
    total = sum(w[k] for k in present)
    return sum(w[k] * v for k, v in present.items()) / total if total else None


def verdict_for(score: float, threshold: float) -> str:
    return "favourable" if score >= threshold else "unfavourable" if score <= -threshold else "neutral"


def separation(rows: list[dict], weights: dict, threshold: float, factor: float) -> float | None:
    fav, rest = [], []
    for r in rows:
        s = weighted(r.get("sub_scores"), weights, r.get("low_reliability"), factor)
        if s is None:
            continue
        (fav if verdict_for(s, threshold) == "favourable" else rest).append(r["excess"])
    if len(fav) < MIN_GROUP or len(rest) < MIN_GROUP:
        return None
    return mean(fav) - mean(rest)


def _in_test(r: dict, share: float) -> bool:
    digest = hashlib.sha256(f"{r.get('ticker')}|{r.get('as_of')}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF < share


def suggest(scored: list[dict]) -> dict:
    """{"status", "lines", ...}: whether a different setting would have separated good
    and bad picks better, checked on held-out verdicts."""
    cfg, card = config(), scorecard_config()
    rows = [r for r in scored if r.get("sub_scores") and r.get("excess") is not None]
    need = cfg["min_verdicts_for_tuning"]
    if len(rows) < need:
        return {"status": "not_enough_data",
                "lines": [f"not yet: {len(rows)} of the {need} scored verdicts with sub-scores needed"]}
    factor = card.get("low_reliability_weight_factor", 1.0)
    test = [r for r in rows if _in_test(r, cfg["holdout_share"])]
    train = [r for r in rows if not _in_test(r, cfg["holdout_share"])]
    now_w, now_t = card["weights"], card["verdict_thresholds"]["favourable_above"]

    results = []
    for (name, w), t in product(WEIGHT_SETS.items(), THRESHOLDS):
        w = w or now_w
        results.append({"weights": name, "threshold": t, "train": separation(train, w, t, factor),
                        "test": separation(test, w, t, factor), "w": w})
    current = next(r for r in results if r["weights"] == "current" and r["threshold"] == now_t)
    usable = [r for r in results if r["train"] is not None]
    lines = []
    cur = (f"{current['train'] * 100:+.1f} points on the 70%, "
           + (f"{current['test'] * 100:+.1f} on the held-out 30%" if current["test"] is not None
              else "too few favourable picks in the held-out 30% to judge")
           if current["train"] is not None else "too few favourable picks to judge")
    lines.append(f"today's settings (weights 50/20/15/5/10, line at +-{now_t:g}): favourable picks beat the rest by {cur}")
    if not usable:
        return {"status": "no_usable_setting", "lines": lines + ["no setting gave enough favourable picks to compare"],
                "results": _clean(results)}
    best = max(usable, key=lambda r: r["train"])
    lines.append(f"best on the 70%: {best['weights']} weights, line at +-{best['threshold']:g}, "
                 f"{best['train'] * 100:+.1f} points"
                 + (f", {best['test'] * 100:+.1f} on the held-out 30%" if best["test"] is not None else ""))
    # A change must actually work on the held-out verdicts (favourable picks beating the
    # rest), not only lose by less than today's settings: the 7 October replay "suggested"
    # a setting that lost by 15 points because today's lost by 19.
    works = best["test"] is not None and best["test"] > 0
    better = (best is not current and works
              and (current["test"] is None or best["test"] >= current["test"] + MIN_GAIN))
    if better:
        lines.append("suggestion: try these settings as scorecard version "
                     f"{int(card.get('version', 1)) + 1} (needs your approval; nothing is changed automatically)")
        status = "suggestion"
    elif not works:
        lines.append("no change suggested: under no setting tried did favourable picks beat the rest on the "
                     "held-out verdicts, so the problem is not the weights or the line")
        status = "no_setting_works"
    else:
        lines.append("no change suggested: nothing beat today's settings by 2 points on the held-out verdicts")
        status = "keep"
    return {"status": status, "lines": lines, "best": {k: v for k, v in best.items()}, "results": _clean(results)}


def _clean(results: list[dict]) -> list[dict]:
    return [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k != "w"} for r in results]
