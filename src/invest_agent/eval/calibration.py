"""
eval/calibration.py: do the confidence levels mean anything? (design 14.11, 14.17)

What this file does:
    "High confidence" should mean the verdict holds up more often than at "medium", and
    medium more often than "low". check() compares the hit rates from scoring.summarise
    once each level has enough scored verdicts (20, backtest_config.yaml) and says one of:

      not_enough_data   fewer than 20 verdicts at some level: no judgement yet, and the
                        report keeps saying confidence is not yet calibrated
      calibrated        high > medium > low, as it should be
      not_calibrated    the order is wrong somewhere; the finding names where, for
                        example "medium verdicts were right more often than high"

    It suggests, it never changes anything: a change to the confidence rules
    (analysis/confidence.py and scorecard_config.yaml) is made by hand after Aakash
    approves it, with a new config version. Calibration figures are never given to the
    model.
"""

from __future__ import annotations

from invest_agent.eval.scoring import config

LEVELS = ("high", "medium", "low")


def check(by_confidence: dict) -> dict:
    need = config()["min_per_confidence"]
    short = [lv for lv in LEVELS if (by_confidence.get(lv) or {}).get("count", 0) < need]
    if short:
        return {"status": "not_enough_data",
                "finding": f"Not yet: {', '.join(short)} confidence {'has' if len(short) == 1 else 'have'} fewer "
                           f"than {need} scored verdicts."}
    rates = {lv: by_confidence[lv]["hit_rate"] for lv in LEVELS}
    problems = [f"{b} verdicts were right more often than {a} ({rates[b] * 100:.0f}% against {rates[a] * 100:.0f}%)"
                for a, b in (("high", "medium"), ("medium", "low")) if rates[b] >= rates[a]]
    if problems:
        return {"status": "not_calibrated", "rates": rates, "finding": "; ".join(problems).capitalize() + "."}
    return {"status": "calibrated", "rates": rates,
            "finding": (f"High {rates['high'] * 100:.0f}%, medium {rates['medium'] * 100:.0f}%, "
                        f"low {rates['low'] * 100:.0f}% right: in the right order.")}
