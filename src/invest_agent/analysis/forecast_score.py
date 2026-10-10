"""
analysis/forecast_score.py: F4, the forecast sub-score (design 14.18).

What this file does:
    Compares the base fair value with today's price:

      base fair value against price   score
      25% or more above               +2
      10% to 25% above                +1
      within 10%                       0
      10% to 25% below                -1
      more than 25% below             -2

    For a large company (fundamental_score.is_large) the downside bands are wider: within
    15% below scores 0, 15% to 35% below -1, more than 35% below -2, since the peers'
    multiples sit below a market leader's.

    No fair value means the sub-score is missing, never zero; the scorecard shares its
    10% weight out among the others. It carries the smallest weight because it is the
    most speculative part of the analysis.
"""

from __future__ import annotations

from invest_agent.analysis.projection import config
from invest_agent.analysis.scoring_common import band, finding, pct, smooth_band


def forecast(fv: dict, *, money, record_ids: list[str] | None = None, large: bool = False) -> dict:
    """money(value) formats a per-share amount in the trading currency; record_ids are the
    projected fair value records the finding points to."""
    if fv.get("status") != "ok" or "base" not in (fv.get("upside") or {}):
        return {"score": None, "findings": [], "missing": [fv.get("reason") or "no fair value"]}
    up = fv["upside"]["base"]
    cfg = config()
    bands = cfg.get("large_company_score_bands", cfg["score_bands"]) if large else cfg["score_bands"]
    # Version 6: the fair value moves with the price every day, so the score slides instead
    # of jumping at a band edge (scoring_common.smooth_band).
    score = smooth_band(up, bands) if cfg.get("smooth_score") else band(up, bands)
    rng = fv["range"]
    low, high = rng.get("low", rng["base"]), rng.get("high", rng["base"])
    side = "above" if up >= 0 else "below"
    text = (f"Estimated fair value a year out (a projection, not a reported figure): {money(low)} to {money(high)}, "
            f"base {money(rng['base'])}, {abs(up) * 100:.0f}% {side} today's price of {money(fv['price'])}"
            + (" (large-company bands)" if large else ""))
    return {"score": score, "missing": [],
            "findings": [finding("forecast", score, text,
                                 {"low": money(low), "base": money(rng["base"]), "high": money(high),
                                  "upside": pct(up, 0, signed=True)},
                                 record_ids or [])]}
