"""
analysis/scorecard.py: the scorecard that sets the preliminary verdict (design 14.11).

What this file does:
    1. Insufficient data first: no current price, or fewer than 3 years of revenue and
       net income, means no verdict at all (the partial findings are still shown,
       marked incomplete). A missing budget is not this: it only withholds the amount.
    2. The weighted score: business quality 50, valuation 20, macro fit 15, news 5,
       forecast 10. A missing sub-score's weight is shared out among the others in
       proportion, and the gap is listed. If every sub-score is missing, the verdict
       is insufficient data.
    3. The verdict: +0.75 or above favourable, -0.5 or below unfavourable (version 5;
       -0.75 before), otherwise neutral. The lines live in scorecard_config.yaml.
    4. Hard overrides cap the verdict at neutral, whatever the score:
         a recent 8-K for bankruptcy (1.03) or restatement (4.02)
         a negative news item the labelling marked as a threat to the business itself
       The suitability rules (suitability.py) can cap it the same way.

    5. A weak peer comparison (A14, 6 October): when the company's P/E or EV/EBITDA is
       more than 100% above the peer median, the peers usually cover only part of its
       business (a conglomerate). The valuation and forecast scores, which both rest on
       those peers, are then marked low reliability, and confidence drops one step.

    The model in step 10 may move this verdict by one step with a sourced reason, but
    never past a cap and never out of insufficient data.
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.verification.record_index import RecordIndex

ORDER = ["unfavourable", "neutral", "favourable"]


def config() -> dict:
    return load_yaml("analysis/scorecard_config.yaml")


def insufficient(records: list[dict]) -> list[str]:
    """Reasons there can be no verdict; empty when the analysis can go on."""
    cfg = config()["insufficient_data"]
    idx = RecordIndex(records)
    reasons = []
    if cfg["needs_price"] and idx.value("current_price") is None:
        reasons.append("there is no current share price")
    years = set(idx.years("revenue")) & set(idx.years("net_income"))
    if len(years) < cfg["min_years"]:
        reasons.append(f"fewer than {cfg['min_years']} years of revenue and net income ({len(years)} found)")
    return reasons


def weighted(sub_scores: dict[str, float | None], low_reliability: list[str] | None = None) -> dict:
    """{score, weights_used (shares of 1), missing}; score None when nothing is present.
    A sub-score in low_reliability counts at low_reliability_weight_factor of its weight."""
    cfg = config()
    factor = cfg.get("low_reliability_weight_factor", 1.0)
    weights = {k: w * (factor if k in (low_reliability or []) else 1.0) for k, w in cfg["weights"].items()}
    present = {k: v for k, v in sub_scores.items() if v is not None and k in weights}
    missing = [k for k in weights if k not in present]
    total = sum(weights[k] for k in present)
    if not total:
        return {"score": None, "weights_used": {}, "missing": missing}
    used = {k: weights[k] / total for k in present}
    return {"score": round(sum(used[k] * v for k, v in present.items()), 4),
            "weights_used": {k: round(w, 4) for k, w in used.items()}, "missing": missing}


def verdict_from(score: float) -> str:
    t = config()["verdict_thresholds"]
    if score >= t["favourable_above"]:
        return "favourable"
    if score <= t["unfavourable_below"]:
        return "unfavourable"
    return "neutral"


def overrides(news: dict | None) -> list[dict]:
    """The hard overrides that apply: [{rule, text, record_ids}]."""
    cfg = config()["hard_overrides"]
    news = news or {}
    out = []
    for f in news.get("serious_filings") or news.get("filings") or []:
        items = [i for i in cfg["serious_8k_items"] if i in (f.get("items") or [])] if f.get("items") else []
        if f.get("serious") or items:
            out.append({"rule": "serious_filing", "text": f"8-K filed {f.get('date')}: {f.get('title')}",
                        "record_ids": [f"sec_edgar:{news.get('ticker', '')}:8k_filing:{f.get('accession')}"]})
    if cfg["news_threatens_business"]:
        serious_ids = set((news.get("summary") or {}).get("serious_negative_ids") or [])
        for item in news.get("items", []):
            if item.get("id") in serious_ids:
                out.append({"rule": "serious_news", "text": f"{item.get('date')} {item.get('outlet')}: {item.get('title')}",
                            "record_ids": [f"{item.get('origin', 'news')}:{news.get('ticker', '')}:news_item:{item['id']}"]})
    return out


def cap(verdict: str, at: str = "neutral") -> str:
    """Lower a verdict to `at` if it is above it; never raises one."""
    if verdict not in ORDER:
        return verdict
    return ORDER[min(ORDER.index(verdict), ORDER.index(at))]


def near_line(card: dict | None) -> str | None:
    """A plain note when the weighted score sits within near_line_margin of a verdict line
    (version 6, 10 October: Johnson & Johnson at +0.74 against the +0.75 favourable line,
    Microsoft moving across it with the price). None when it is not close, or when a cap
    or the reasoning step decided the verdict instead of the line."""
    card = card or {}
    score = card.get("score")
    if score is None or card.get("status") not in (None, "ok") or card.get("capped"):
        return None
    t = config()["verdict_thresholds"]
    margin = config().get("near_line_margin", 0.1)
    for line, name in ((t["favourable_above"], "favourable"), (t["unfavourable_below"], "unfavourable")):
        if abs(score - line) < margin:
            return (f"The weighted score ({score:+.2f}) is close to the {name} line ({line:+g}), so a small "
                    f"change in the share price or the next results could move the verdict.")
    return None


# Why a sub-score is marked low reliability; the step keeps one reason per sub-score in
# the scorecard (low_reliability_reasons) so the report and the reasoning say the right one.
WEAK_PEERS_REASON = "weak peer comparison"


def reliability_reason(card: dict | None, name: str) -> str | None:
    """The reason a sub-score is low reliability, or None when it is not."""
    card = card or {}
    if name not in (card.get("low_reliability") or []):
        return None
    return (card.get("low_reliability_reasons") or {}).get(name) or WEAK_PEERS_REASON


def weak_peers(valuation: dict | None) -> list[tuple[str, float]]:
    """[(multiple name, premium)] for each multiple far above the peer median; empty if none."""
    limit = config().get("weak_peer_premium", 1.0)
    peers = (valuation or {}).get("peers") if isinstance(valuation, dict) else None
    comps = (peers.get("comparisons") if isinstance(peers, dict) else None) or {}
    names = {"pe": "P/E", "ev_ebitda": "EV/EBITDA"}
    return [(names[m], c["premium"]) for m, c in comps.items()
            if m in names and c.get("premium") is not None and c["premium"] > limit]
