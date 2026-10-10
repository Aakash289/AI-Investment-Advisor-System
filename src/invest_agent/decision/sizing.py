"""
decision/sizing.py: Position Sizing, how much and how many shares (design 14.13).

What this file does:
    Turns the checked verdict and confidence into an amount, in six steps, keeping every
    step's input and result for the report's "how this was worked out" page:

      1 full position    the concentration limit (the person's own, or the default for
                         their risk tolerance: low 10%, medium 15%, high 25% of the
                         budget); the most the analysis would put into one stock
      2 share of it      favourable 60% to 100% of the full position, neutral 30% to 60%,
                         by where the weighted score sits in its verdict's band, then
                         x 1.0, 0.9 or 0.8 for high, medium or low confidence;
                         unfavourable nothing (version 2, 6 October, replacing a flat
                         share of the budget that read as a token amount)
      3 volatility       amount x (target volatility / the stock's volatility), never
                         scaled up: low risk tolerance targets 20% a year, medium 30%,
                         high 45%, so a cautious person's amount shrinks for a stock that
                         swings more than average
      4 whole shares     the amount in the stock's currency divided by today's price,
                         rounded down; if one share costs more than the amount, it says so
                         (and that some US brokers allow fractional shares)
      5 liquidity        a warning if the amount is more than 1% of the stock's average
                         daily traded value
      6 currency         a budget in rupees for a dollar stock is converted at the official
                         reference rate before the whole-share step, and the result
                         converted back for display, marked indicative (numbered in the
                         order the steps run, so it shows as step 4 when it applies)

    No amount at all, with the reason, when: the verdict is insufficient data, the
    verification step withheld the amount (critical data missing, completeness under
    70%), or no budget was given. The amount always assumes the person does not already
    own the stock, and says so.

    Plain code, no model call, no outside call.
"""

from __future__ import annotations

import math

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.decision.concentration import apply_cap
from invest_agent.fx.convert import format_money
from invest_agent.graph.state import GraphState
from invest_agent.verification.record_index import RecordIndex

HOLDING_NOTE = "This amount assumes you do not already own this stock."


def config() -> dict:
    return load_yaml("decision/sizing_config.yaml")


def _withheld(reason: str, steps: list[dict]) -> dict:
    return {"status": "withheld", "reason": reason, "steps": steps, "amount": None, "shares": None,
            "notes": [f"No amount is given because {reason}."]}


def size(state: dict) -> dict:
    """Returns {"status", "steps", "amount" (budget currency), "amount_stock", "shares",
    "currency", "stock_currency", "notes", "overexposure_note", "liquidity_note"}."""
    cfg = config()
    profile = state.get("user_profile") or {}
    verdict = state.get("verdict") or state.get("preliminary_verdict")
    confidence = state.get("confidence") or state.get("preliminary_confidence") or "low"
    budget, budget_ccy = profile.get("budget_amount"), profile.get("budget_currency")
    stock_ccy = state.get("listing_currency") or budget_ccy
    steps: list[dict] = []

    if verdict == "insufficient_data":
        return _withheld("the data was not complete enough for a verdict", steps)
    if state.get("amount_withheld"):
        return _withheld(state.get("amount_withheld_reason") or "the data was not complete enough", steps)
    if not budget:
        return _withheld("no budget was provided", steps)

    # Step 1: the full position, the concentration limit.
    cap = apply_cap(0.0, budget, budget_ccy, profile)
    full = cap["limit_amount"]
    whose = "the default for your risk tolerance" if cap["default"] else "your own limit"
    steps.append({"step": 1, "name": "full position",
                  "text": f"A full position is {cap['limit'] * 100:g}% of {format_money(budget, budget_ccy)} = "
                          f"{format_money(full, budget_ccy)} ({whose}): the most this analysis would put into "
                          f"one stock"})

    # Step 2: the share of a full position, from the verdict, the score and confidence.
    lo, hi = cfg["share_of_full_position"].get(verdict, [0, 0])
    score = (state.get("scorecard") or {}).get("score")
    edges = cfg["score_bands"].get(verdict)
    if edges and score is not None:
        where = min(1.0, max(0.0, (score - edges[0]) / (edges[1] - edges[0])))
    else:
        where = 0.5                                      # no score (or a band without edges): the middle
    factor = cfg["confidence_factor"].get(confidence, 0.8)
    share = (lo + (hi - lo) * where) * factor
    amount = full * share
    if hi == 0:
        steps.append({"step": 2, "name": "share of a full position",
                      "text": f"{verdict.capitalize()}: none of a full position"})
        return {"status": "zero", "reason": f"the verdict is {verdict}", "steps": steps, "amount": 0.0,
                "amount_stock": 0.0, "shares": 0, "currency": budget_ccy, "stock_currency": stock_ccy,
                "full_position": full, "share_of_full": 0.0,
                "notes": [f"No amount is suggested: an {verdict} verdict means the analysis argues against "
                          f"putting money into this stock now."], "overexposure_note": None, "liquidity_note": None}
    score_text = (f"weighted score {score:+.2f} sits {where * 100:.0f}% of the way up the {verdict} band, "
                  if score is not None else "")
    steps.append({"step": 2, "name": "share of a full position",
                  "text": f"{verdict.capitalize()} ({lo * 100:g}% to {hi * 100:g}% of a full position): "
                          f"{score_text}giving {(lo + (hi - lo) * where) * 100:.0f}% of a full position; {confidence} confidence "
                          f"x {factor:.2f} "
                          f"= {share * 100:.0f}% = {format_money(amount, budget_ccy)}"})

    # Step 2: volatility scaling, never upwards.
    idx = RecordIndex(state.get("verified_records") or [])
    risk = profile.get("risk_tolerance") or "medium"
    target = cfg["risk"][risk]["target_volatility"]
    vol = idx.value("volatility_1y")
    flags = []
    if vol:
        mult = min(1.0, target / vol)
        steps.append({"step": 3, "name": "volatility",
                      "text": f"Target {target * 100:g}% a year for {risk} risk tolerance, the stock swings "
                              f"{vol * 100:.1f}%: x {mult:.2f} = {format_money(amount * mult, budget_ccy)}"})
        amount *= mult
    else:
        flags.append("no_volatility")
        steps.append({"step": 3, "name": "volatility", "text": "Volatility is not available, so no scaling"})

    # The concentration limit cannot be passed: the amount is at most a full position.
    amount = min(amount, full)

    # Step 6 (done before 4): into the stock's currency.
    rate_info = (state.get("fx") or {}).get("rate") or {}
    rate = rate_info.get("rate")
    if stock_ccy != budget_ccy:
        if not rate:
            return _withheld(f"no exchange rate between {budget_ccy} and {stock_ccy} could be found", steps)
        amount_stock = amount / rate                     # rate is budget currency per unit of stock currency
        steps.append({"step": len(steps) + 1, "name": "currency",
                      "text": f"{format_money(amount, budget_ccy)} at {rate:.2f} {budget_ccy} per {stock_ccy} = "
                              f"{format_money(amount_stock, stock_ccy)} (indicative, official reference rate)"})
    else:
        amount_stock = amount

    # Step 4: whole shares.
    price = idx.value("current_price")
    notes = [HOLDING_NOTE]
    if cap["default"]:
        notes.append(f"The full position uses the default limit for your risk tolerance ({cap['limit'] * 100:g}% "
                     f"of your budget); tell me your own limit to change it.")
    # Context for the rest of the budget: the stock's 5-year growth beside the index's.
    own, index_growth = idx.value("price_cagr_5y"), idx.value("benchmark_cagr_5y")
    if own is not None and index_growth is not None:
        bench = {"^NSEI": "Nifty 50", "^GSPC": "S&P 500"}.get((state.get("price_chart") or {}).get("benchmark_symbol"),
                                                                "the market index")
        notes.append(f"Over the last 5 years the share price grew {own * 100:+.1f}% a year, against "
                     f"{index_growth * 100:+.1f}% a year for the {bench}. Past growth does not predict future "
                     f"growth.")
    if not price:
        return _withheld("there is no current share price", steps)
    shares = math.floor(amount_stock / price)
    invested_stock = shares * price
    invested = invested_stock * rate if stock_ccy != budget_ccy and rate else invested_stock
    if shares == 0:
        msg = f"One share costs {format_money(price, stock_ccy)}, more than the {format_money(amount_stock, stock_ccy)} suggested"
        price_budget = price * rate if stock_ccy != budget_ccy and rate else price
        if price_budget > full:
            msg += (f". It also costs more than a full position for your profile "
                    f"({format_money(full, budget_ccy)}), so no whole share fits your limit")
        if stock_ccy == "USD":
            msg += ("; some platforms for buying US shares from India sell part of a share"
                    if budget_ccy == "INR" else "; some brokers allow buying part of a share")
        notes.append(msg + ".")
    # The full-position note, on the amount actually shown: the whole shares bought, or the
    # suggested amount when not even one share fits.
    shown = invested if shares else amount
    share_now, rest = (shown / full if full else 0.0), full - shown
    if verdict == "neutral":
        notes.insert(0, f"A full position for your profile is {format_money(full, budget_ccy)}. The verdict is "
                        f"neutral (the strengths and weaknesses roughly balance), so about {share_now * 100:.0f}% of it "
                        f"is suggested now. If a later analysis turns favourable, up to "
                        f"{format_money(rest, budget_ccy)} more would fit within the full position.")
    elif verdict == "favourable" and rest > 1:
        notes.insert(0, f"A full position for your profile is {format_money(full, budget_ccy)}; about "
                        f"{share_now * 100:.0f}% of it is suggested now, given the score, confidence and volatility.")
    steps.append({"step": len(steps) + 1, "name": "whole shares",
                  "text": f"{format_money(amount_stock, stock_ccy)} / {format_money(price, stock_ccy)} a share = "
                          f"{shares} shares, {format_money(invested_stock, stock_ccy)}"})

    # Step 5: liquidity.
    liquidity_note = None
    daily = idx.value("avg_traded_value_3m")
    if daily:
        share_of_daily = invested_stock / daily
        if share_of_daily > cfg["liquidity_share_of_daily_value"]:
            liquidity_note = (f"This amount is {share_of_daily * 100:.1f}% of the stock's average daily trading, "
                              f"so buying or selling it could move the price.")
        pct_text = f"{share_of_daily * 100:.1f}%" if share_of_daily >= 0.001 else "well under 0.1%"
        steps.append({"step": len(steps) + 1, "name": "liquidity",
                      "text": f"{pct_text} of the stock's average daily trading: "
                              + ("warning" if liquidity_note else "no issue")})

    suggested = amount                                    # before rounding to whole shares, budget currency
    return {"status": "ok", "steps": steps, "amount": invested, "amount_stock": invested_stock, "shares": shares,
            "suggested": suggested, "suggested_stock": amount_stock, "full_position": full,
            "share_of_full": suggested / full if full else None,
            "currency": budget_ccy, "stock_currency": stock_ccy, "price": price, "notes": notes,
            "overexposure_note": None, "liquidity_note": liquidity_note, "flags": flags}


def sizing_node(state: GraphState) -> dict:
    s = size(state)
    return {"sizing": s, "sizing_steps": s["steps"], "recommended_amount": s.get("amount"),
            "recommended_currency": s.get("currency"), "recommended_shares": s.get("shares"),
            "overexposure_note": s.get("overexposure_note") or "", "liquidity_note": s.get("liquidity_note") or ""}
