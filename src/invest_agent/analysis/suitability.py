"""
analysis/suitability.py: does this stock suit this person (design 14.11)?

What this file does:
    Checks the stock against the user's profile. The scorecard says whether the
    company looks good; these rules say whether it fits the person:

      profile                      stock                         effect
      goal: capital preservation   volatility above 35% a year,  verdict capped at neutral
                                   or net debt above 3x EBITDA
      goal: income                 no dividend                   verdict capped at neutral
      horizon under 3 years        any stock                     short-horizon caution
                                   volatility above 35% a year   verdict capped at neutral
      liquidity needs stated       thinly traded                 liquidity warning

    A company with net cash passes the debt test, and banks and insurers skip it (their
    debt is their raw material). Volatility is used here and in position sizing, not in
    the scorecard, so it is not counted three times. A rule whose profile field is
    missing is skipped and noted rather than guessed.
"""

from __future__ import annotations

from invest_agent.analysis.scorecard import config
from invest_agent.verification.record_index import RecordIndex


def check(profile: dict | None, records: list[dict], *, bank_mode: bool) -> dict:
    """Returns {"caps": [{rule, text}], "notes": [text], "skipped": [rule]}."""
    cfg = config()["suitability"]
    profile = profile or {}
    idx = RecordIndex(records)
    caps, notes, skipped = [], [], []

    vol = idx.value("volatility_1y")
    high_vol = vol is not None and vol > cfg["high_volatility_annualised"]
    year = idx.latest_year()
    net_debt = idx.value("net_debt", year) if year else None
    nde = idx.value("net_debt_to_ebitda", year) if year else None
    high_debt = (not bank_mode and net_debt is not None and net_debt > 0
                 and nde is not None and nde > cfg["high_debt_net_debt_to_ebitda"])

    goal = profile.get("goal")
    if goal == "capital_preservation":
        reasons = []
        if high_vol:
            reasons.append(f"its price has swung {vol * 100:.0f}% a year, above the 35% mark")
        if high_debt:
            reasons.append(f"its net debt is {nde:.1f} times EBITDA, above 3 times")
        if reasons:
            caps.append({"rule": "capital_preservation",
                         "text": "Your goal is to protect your capital, and " + " and ".join(reasons)})
    elif goal == "income":
        dividends = idx.value("dividends_paid", year) if year else None
        dps = idx.value("dividend_per_share_1y")
        pays = (dividends is not None and abs(dividends) > 0) or (dps is not None and dps > 0)
        if not pays:
            caps.append({"rule": "income", "text": "Your goal is income, and the company paid no dividend last year"})
    elif goal is None:
        skipped.append("goal")

    horizon = profile.get("horizon_years")
    if horizon is None:
        skipped.append("horizon")
    elif horizon < cfg["short_horizon_years"]:
        notes.append(f"Your horizon is {horizon:g} years; over a short horizon any share price can fall and "
                     f"not recover in time")
        if high_vol:
            caps.append({"rule": "short_horizon", "text": f"Your horizon is under {cfg['short_horizon_years']} years "
                         f"and the price has swung {vol * 100:.0f}% a year"})

    if profile.get("liquidity_needs"):
        thin = idx.value("thinly_traded")
        if thin:
            notes.append("You may need this money at short notice, and the stock trades thinly, so selling "
                         "quickly could mean accepting a lower price")
    return {"caps": caps, "notes": notes, "skipped": skipped}
