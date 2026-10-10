"""
analysis/step.py: the Analysis node (build step 9, design 14.10, 14.11 and 14.18).

What this file does:
    Runs after verification and turns the checked figures into sub-scores, each from
    -2 to +2, and plain-language findings that later steps can cite:

      business_quality   F1, from the verified annual figures (fundamental_score.py)
      valuation          F1, from verification's peer comparison (fundamental_score.py)
      macro_fit          F2, economy regimes x sector sensitivity x revenue by economy
                         (macro_score.py)
      news               F3, the News agent's net sentiment (news_score.py)
      forecast           F4, base fair value a year out against today's price
                         (forecasting.py, projection.py, fair_value.py, forecast_score.py)

    A sub-score that cannot be worked out is None, never a guess; the scorecard shares
    its weight out among the others.

    Then the decision (9c): insufficient data first; otherwise the weighted scorecard
    (scorecard.py), the hard overrides and the suitability caps (suitability.py), each
    able to cap the verdict at neutral, and the confidence level (confidence.py). The
    result is the preliminary verdict, which Synthesis (step 10) reviews.

    Only plain code: no model is called here, so the same figures always give the same
    scores, and every score can be traced to a band in scoring_bands.yaml.

State it reads:  verified_records, valuation, macro_panel, geo_exposure, sector, industry,
                 market, news, bank_mode, completeness_detail, user_profile
State it writes: sub_scores, findings, analysis_detail, projection, fair_value,
                 projection_records (every projected number, tagged "projected"),
                 scorecard, suitability_notes, preliminary_verdict, preliminary_confidence
"""

from __future__ import annotations

import logging

from invest_agent.analysis import (
    confidence,
    forecast_score,
    fundamental_score,
    macro_score,
    news_score,
    scorecard,
    suitability,
)
from invest_agent.analysis.forecasting import forecast
from invest_agent.fx.convert import format_money
from invest_agent.graph.state import GraphState

logger = logging.getLogger(__name__)


def _industry(state: GraphState) -> str:
    """Trendlyne's industry (India) and Yahoo's, joined, so an override can match either."""
    detail = (state.get("geo_exposure") or {}).get("sector_detail") or {}
    return " | ".join(x for x in (state.get("industry"), detail.get("yahoo_industry")) if x)


def analyse(state: GraphState) -> dict:
    """Work out the four sub-scores and their findings from the state."""
    bank = bool(state.get("bank_mode"))
    records = state.get("verified_records") or []
    unbroken = (state.get("completeness_detail") or {}).get("unbroken_years")
    # Large companies are scored on wider valuation and forecast bands (version 2).
    large = fundamental_score.is_large(state.get("valuation"), state.get("listing_currency"))

    # Each scorer is wrapped so one failure leaves that sub-score missing, not the request.
    def safe(name, fn, *args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except Exception as err:
            logger.warning("Scoring %s failed: %s", name, err)
            return {"score": None, "findings": [], "missing": [f"{name} failed"]}

    parts = {
        "business_quality": safe("business_quality", fundamental_score.business_quality, records,
                                 bank_mode=bank, unbroken_years=unbroken),
        "valuation": safe("valuation", fundamental_score.valuation,
                          (state.get("valuation") or {}).get("peers"), bank_mode=bank, large=large),
        "macro_fit": safe("macro_fit", macro_score.macro_fit, state.get("macro_panel"), state.get("geo_exposure"),
                          sector=state.get("sector"), industry=_industry(state), market=state.get("market")),
        "news": safe("news", news_score.news, state.get("news")),
    }
    # F4: projection and fair value, then the sub-score.
    try:
        fc = forecast(records, state.get("valuation"), ticker=state.get("ticker") or "", bank_mode=bank,
                      unbroken_years=unbroken)
    except Exception as err:
        logger.warning("Forecasting failed: %s", err)
        fc = {"projection": {"status": "failed", "flags": ["forecasting_failed"]},
              "fair_value": {"status": "none", "reason": "forecasting failed"}, "records": []}
    # A weak peer comparison (the company far dearer than every peer, as for a conglomerate)
    # makes a fair value at peer multiples misleading, so none is given and the forecast
    # sub-score is missing; its weight is shared out (decided 6 October, after A14).
    weak = scorecard.weak_peers(state.get("valuation"))
    if weak and fc["fair_value"].get("status") == "ok":
        worst = ", ".join(f"{n} {p * 100:.0f}% above the peer median" for n, p in weak)
        fc["fair_value"] = {"status": "none", "weak_peers": True,
                            "reason": f"the peer comparison is too weak to value the company at peer multiples "
                                      f"(its {worst})"}
        fc["records"] = [r for r in fc["records"] if not r["field"].startswith("fair_value_")]
    # The latest year's growth far from the earlier years: the history is a poor guide to
    # next year, so no fair value (the projection itself is still shown, flagged).
    proj_a = (fc["projection"].get("assumptions") or {})
    if "latest_year_unusual" in fc["projection"].get("flags", []) and fc["fair_value"].get("status") == "ok":
        latest = list((proj_a.get("left_out_years") or {}).items())[-1]
        fc["fair_value"] = {"status": "none", "latest_year_unusual": True,
                            "reason": f"the latest year's revenue growth ({latest[0]}: {latest[1] * 100:+.1f}%) is far "
                                      f"from the earlier years, so the past is a poor guide to next year"}
        fc["records"] = [r for r in fc["records"] if not r["field"].startswith("fair_value_")]
    currency = state.get("listing_currency") or "USD"
    fv_ids = [r["record_id"] for r in fc["records"] if r["field"].startswith("fair_value_")]
    parts["forecast"] = safe("forecast", forecast_score.forecast, fc["fair_value"],
                             money=lambda v: format_money(v, currency), record_ids=fv_ids, large=large)

    sub_scores = {name: p.get("score") for name, p in parts.items()}
    findings = [f for p in parts.values() for f in p.get("findings", [])]
    detail = {name: {k: v for k, v in p.items() if k != "findings"} for name, p in parts.items()}
    detail["large_company"] = large
    return {"sub_scores": sub_scores, "findings": findings, "analysis_detail": detail,
            "projection": fc["projection"], "fair_value": fc["fair_value"], "projection_records": fc["records"],
            **decide(state, sub_scores, valuation_fallback=parts["valuation"].get("fallback"))}


def peer_caveats(valuation: dict | None) -> dict[str, str]:
    """{"valuation": reason, "forecast": reason} when the peer medians are weaker than usual:
    peers taken from the wider sector, or medians of only 3 or 4 peers (version 6)."""
    peers = (valuation or {}).get("peers") if isinstance(valuation, dict) else None
    if not isinstance(peers, dict):
        return {}
    parts = []
    if peers.get("widened_to"):
        parts.append(f"peers from the wider {peers['widened_to']} sector, as its own industry has too few")
    few = sorted({c.get("peers_used") for c in (peers.get("comparisons") or {}).values() if c.get("few_peers")})
    if few:
        parts.append(f"a median of only {' or '.join(str(n) for n in few)} peers")
    if not parts:
        return {}
    why = "; ".join(parts)
    return {"valuation": why, "forecast": why}


def decide(state: GraphState, sub_scores: dict, valuation_fallback: list[str] | None = None) -> dict:
    """9c: the preliminary verdict and confidence, with every step kept for the report."""
    records = state.get("verified_records") or []
    cfg_version = scorecard.config().get("version")
    gaps = scorecard.insufficient(records)
    # A14: a weak peer comparison makes valuation and forecast low reliability; since
    # version 2 they also count at half weight.
    weak = scorecard.weak_peers(state.get("valuation"))
    reasons_low = {k: scorecard.WEAK_PEERS_REASON for k in ("valuation", "forecast")
                   if weak and sub_scores.get(k) is not None}
    # A valuation scored only on P/B or P/S (no P/E or EV/EBITDA median) is low reliability too.
    if valuation_fallback and sub_scores.get("valuation") is not None:
        names = {"pb": "P/B", "ps": "P/S"}
        reasons_low.setdefault("valuation", "only " + " and ".join(names.get(m, m) for m in valuation_fallback)
                               + " could be compared with peers, as too few of them make a profit")
    # Version 6 (10 October, small industries): peers from the wider sector, or a median of
    # only 3 or 4 peers, make the valuation and the fair value built on it low reliability.
    for k, why in peer_caveats(state.get("valuation")).items():
        if sub_scores.get(k) is not None:
            reasons_low.setdefault(k, why)
    low_reliability = list(reasons_low)
    w = scorecard.weighted(sub_scores, low_reliability)
    if not gaps and w["score"] is None:
        gaps = ["no sub-score could be worked out"]
    if gaps:
        card = {"status": "insufficient_data", "reasons": gaps, **w, "config_version": cfg_version}
        return {"scorecard": card, "suitability_notes": [], "preliminary_verdict": "insufficient_data",
                "preliminary_confidence": "low"}

    raw = scorecard.verdict_from(w["score"])
    verdict = raw
    caps = [{**o, "kind": "override"} for o in scorecard.overrides(state.get("news"))]
    suit = suitability.check(state.get("user_profile"), records, bank_mode=bool(state.get("bank_mode")))
    caps += [{**c, "kind": "suitability"} for c in suit["caps"]]
    if caps:
        verdict = scorecard.cap(verdict)
    conf = confidence.assess(sub_scores, verdict,
                             completeness_decision=(state.get("completeness_detail") or {}).get("decision"))
    if low_reliability:
        lowered = confidence.lower(conf["level"])
        if lowered != conf["level"]:
            if weak:
                why = ("the valuation rests on a weak peer comparison"
                       + (" and so does the forecast" if "forecast" in low_reliability else ", so no fair value is given"))
            else:
                which = "valuation" if "valuation" in reasons_low else next(iter(reasons_low))
                why = f"the {'valuation' if which == 'valuation' else 'forecast'} is low reliability ({reasons_low[which]})"
            conf = {**conf, "level": lowered, "reasons": conf["reasons"] + [f"lowered one step: {why}"]}
    card = {"status": "ok", **w, "raw_verdict": raw, "verdict": verdict, "caps": caps,
            "capped": verdict != raw, "confidence": conf, "suitability_skipped": suit["skipped"],
            "low_reliability": low_reliability, "low_reliability_reasons": reasons_low,
            "config_version": cfg_version}
    notes = [c["text"] for c in suit["caps"]] + suit["notes"]
    return {"scorecard": card, "suitability_notes": notes, "preliminary_verdict": verdict,
            "preliminary_confidence": conf["level"]}


def analysis_node(state: GraphState) -> dict:
    """The graph node. Synchronous: everything it needs is already in the state."""
    return analyse(state)
