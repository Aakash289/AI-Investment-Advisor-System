"""
agents/synthesis_facts.py: everything Synthesis may say, as numbered facts (design 14.11, 14.12).

What this file does:
    Turns the state into a list of short facts, each with an id (F1, F2...), the block it
    belongs to, its text with every number already formatted, the source record IDs
    behind it, and its kind (reported, computed, projection, profile, scorecard, note).

    The model sees only these facts and must cite them as [F3] after any sentence that
    uses a number. The grounding guardrail then checks every number in a sentence
    against the numbers written in the facts that sentence cites. Short ids are easier
    for the model to copy exactly than long record IDs, and each fact keeps its record
    IDs, so the report can still list the real sources (decided 6 October, build step 10).

    Blocks, in the fixed order the design sets (14.11):
      1 profile      the person's profile and budget, the currency note
      2 scorecard    weighted score, preliminary verdict, confidence, caps, suitability
      3 business     business quality findings and the latest year's headline figures
      4 valuation    each multiple against its peer median, the peers, and a warning
                     when the comparison looks weak (A1: a conglomerate such as Reliance
                     compared only with companies in one of its industries)
      5 forecast     the fair value range and its assumptions, always a projection
      6 macro        each economy's effect on this company
      7 news         the news score and the top items (secondary to everything above)
      8 notes        data quality notes, known gaps, projection flags
"""

from __future__ import annotations

from invest_agent.analysis.projection import commodity_note
from invest_agent.analysis.scorecard import reliability_reason, weak_peers
from invest_agent.analysis.target import describe as describe_target
from invest_agent.fx.convert import format_large, format_money
from invest_agent.profile.budget_parser import format_amount
from invest_agent.verification.record_index import RecordIndex

BLOCKS = ["profile", "scorecard", "business", "valuation", "forecast", "macro", "news", "notes"]
BLOCK_TITLES = {"profile": "1. The person's profile", "scorecard": "2. Scorecard result",
                "business": "3. Business quality (the foundation)", "valuation": "4. Valuation and peers",
                "forecast": "5. Forecast (a projection, not a reported figure)", "macro": "6. Economic context",
                "news": "7. News (secondary)", "notes": "8. Data notes and known gaps"}
SUB_NAMES = {"business_quality": "Business quality", "valuation": "Valuation", "macro_fit": "Economic fit",
             "news": "News", "forecast": "Forecast"}
VERDICT_WORDS = {"favourable": "favourable", "neutral": "neutral", "unfavourable": "unfavourable",
                 "insufficient_data": "insufficient data"}
FLAG_TEXT = {"one_unusual_year": "One year's revenue growth was far from the others (a one-off boom or an "
                                 "acquisition) and was left out of the projection's spread",
             "unusual_years": "Several years' revenue growth were far from the others and were left out of the "
                              "projection's spread",
             "unusual_margin_years": "One or more years' net margin were far from the others (a pandemic loss or a "
                                     "one-off gain) and were left out of the projection's low and high cases",
             "latest_year_unusual": "The latest year's revenue growth was far from the earlier years (possibly a "
                                    "change in how revenue is counted, or a step change in the business), so the "
                                    "projection rests on older years and no fair value is given"}


class FactList:
    def __init__(self) -> None:
        self.facts: list[dict] = []

    def add(self, block: str, text: str, record_ids: list[str] | None = None, kind: str = "computed") -> str:
        fid = f"F{len(self.facts) + 1}"
        self.facts.append({"id": fid, "block": block, "text": text, "record_ids": [r for r in (record_ids or []) if r],
                           "kind": kind})
        return fid


def _findings(state: dict, prefixes: tuple[str, ...]) -> list[dict]:
    return [f for f in state.get("findings") or [] if f["component"].startswith(prefixes)]


def weak_peer_note(valuation: dict | None) -> str | None:
    """A1: a company far dearer than its whole peer group usually has peers that cover
    only part of its business. Returns the note, or None."""
    high = weak_peers(valuation)
    if not high:
        return None
    worst = ", ".join(f"{n} {p * 100:.0f}% above the peer median" for n, p in high)
    return (f"The peer comparison looks weak: the company's {worst}. A gap this large usually means the peers "
            f"cover only part of the company's business (for example a conglomerate compared only with companies "
            f"in one of its industries). So the valuation score is marked low reliability, no fair value is "
            f"given (valuing the company at these peers' multiples would mislead), and confidence was lowered "
            f"one step")


def build_facts(state: dict) -> list[dict]:
    fl = FactList()
    currency = state.get("listing_currency") or "USD"
    profile = state.get("user_profile") or {}
    records = state.get("verified_records") or []
    idx = RecordIndex(records)

    # 1. Profile ----------------------------------------------------------------------------------
    budget = (format_amount(profile["budget_amount"], profile.get("budget_currency"))
              if profile.get("budget_amount") else "not given")
    assumed = (f" (assumed: {', '.join(a.replace('_', ' ') for a in profile['assumed_fields'])})"
               if profile.get("assumed_fields") else "")
    horizon = profile.get("horizon_years")
    horizon = f"{horizon:g}" if isinstance(horizon, (int, float)) else horizon
    fl.add("profile", f"Budget {budget}; risk tolerance {profile.get('risk_tolerance')}; horizon "
                      f"{horizon} years; goal {str(profile.get('goal')).replace('_', ' ')}"
                      f"{assumed}", kind="profile")
    target = describe_target(profile)
    if target:
        fl.add("profile", f"Return target stated by the person: {target['text']}", kind="profile")
    fx = state.get("fx") or {}
    if fx.get("needed") and fx.get("risk_note"):
        fl.add("profile", fx["risk_note"], [r["record_id"] for r in state.get("fx_records") or []], kind="reported")

    # 2. Scorecard --------------------------------------------------------------------------------
    card = state.get("scorecard") or {}
    if card.get("status") == "insufficient_data":
        fl.add("scorecard", "Preliminary verdict: insufficient data, because " + "; ".join(card.get("reasons", [])),
               kind="scorecard")
    elif card:
        conf = card.get("confidence") or {}
        fl.add("scorecard", f"Weighted score {card['score']:+.2f} on a scale of -2 to +2; preliminary verdict "
                            f"{VERDICT_WORDS[card['verdict']]}, confidence {conf.get('level')}", kind="scorecard")
        for name, value in (state.get("sub_scores") or {}).items():
            if value is None:
                fl.add("scorecard", f"{SUB_NAMES.get(name, name)} sub-score is missing; its weight was shared out",
                       kind="scorecard")
            else:
                why = reliability_reason(card, name)
                low = f" (low reliability: {why})" if why else ""
                fl.add("scorecard", f"{SUB_NAMES.get(name, name)} sub-score {value:+.2f}{low}", kind="scorecard")
        for c in card.get("caps", []):
            fl.add("scorecard", f"The verdict is capped at neutral: {c['text']}", c.get("record_ids"), kind="scorecard")
    for note in state.get("suitability_notes") or []:
        if not any(note == c.get("text") for c in card.get("caps", [])):
            fl.add("scorecard", note, kind="profile")

    # 3. Business ---------------------------------------------------------------------------------
    year = idx.latest_year()
    if year:
        parts = []
        for field, label in (("revenue", "revenue"), ("net_income", "net profit"), ("free_cash_flow", "free cash flow")):
            v = idx.value(field, year)
            if v is not None:
                parts.append(f"{label} {format_large(v, currency)}")
        if parts:
            ids = [r["record_id"] for f in ("revenue", "net_income", "free_cash_flow")
                   for r in idx.by_field.get(f, []) if r.get("fiscal_year") == year]
            fl.add("business", f"FY{year}: " + ", ".join(parts), ids, kind="reported")
    for f in _findings(state, ("growth", "profitability", "margin_trend", "balance_sheet", "cash_generation")):
        fl.add("business", f"{f['text']} (component score {f['score']:+g})", f["record_ids"])

    # 4. Valuation --------------------------------------------------------------------------------
    peers = ((state.get("valuation") or {}).get("peers") or {})
    if peers.get("peers"):
        left = ", ".join(f"{e['symbol']} ({e['reason']})" for e in peers.get("excluded", []) if e.get("symbol"))
        fl.add("valuation", f"{len(peers['peers'])} peers compared: {', '.join(peers['peers'])}" + (f"; left out: {left}" if left else ""),
               kind="reported")
    for f in _findings(state, ("valuation_",)):
        fl.add("valuation", f"{f['text']} (component score {f['score']:+g})", f["record_ids"])
    weak = weak_peer_note(state.get("valuation"))
    if weak:
        fl.add("valuation", weak, kind="note")

    # 5. Forecast ---------------------------------------------------------------------------------
    proj = state.get("projection") or {}
    proj_ids = [r["record_id"] for r in state.get("projection_records") or []]
    for f in _findings(state, ("forecast",)):
        fl.add("forecast", f"{f['text']} (forecast score {f['score']:+g})", f["record_ids"], kind="projection")
    if proj.get("status") == "ok":
        a = proj["assumptions"]
        y1 = proj["years"][0]
        eps = proj["cases"]["base"][y1].get("eps")
        span = (f" (the average of {a['recent_years'][0]} to {a['recent_years'][-1]})"
                if a.get("recent_years") else "")
        fl.add("forecast", f"Projection assumptions, base case: revenue growth {a['growth']['base'] * 100:.1f}% a year{span} "
                           f"(low case {a['growth']['low'] * 100:.1f}%, high case {a['growth']['high'] * 100:.1f}%), "
                           f"net margin {a['net_margin']['base'] * 100:.1f}%"
                           + (f", EPS FY{y1} {format_money(eps, currency)}" if eps is not None else ""),
               [i for i in proj_ids if f":FY{y1}" in i and "_base" in i], kind="projection")
    fv = state.get("fair_value") or {}
    if fv.get("status") == "none":
        fl.add("forecast", f"There is no fair value, because {fv.get('reason')}", kind="note")

    # 6. Macro ------------------------------------------------------------------------------------
    for f in _findings(state, ("macro_",)):
        fl.add("macro", f"{f['text']} (score {f['score']:+g})", f["record_ids"])

    # 7. News -------------------------------------------------------------------------------------
    for f in _findings(state, ("news",)):
        fl.add("news", f["text"], f["record_ids"])
    news = state.get("news") or {}
    by_id = {i["id"]: i for i in news.get("items", [])}
    ticker = state.get("ticker") or ""
    for item_id in (news.get("summary") or {}).get("top_ids", [])[:3]:
        i = by_id.get(item_id)
        if i:
            fl.add("news", f"{i.get('date')} {i.get('outlet')}: \"{i.get('title')}\" (labelled {i.get('sentiment')}, "
                           f"{i.get('materiality')} materiality)",
                   [f"{i.get('origin', 'news')}:{ticker}:news_item:{item_id}"], kind="reported")

    # 8. Notes ------------------------------------------------------------------------------------
    for note in state.get("data_notes") or []:
        fl.add("notes", note, kind="note")
    line = (state.get("completeness_detail") or {}).get("user_line")
    if line:
        fl.add("notes", line, kind="note")
    for flag in proj.get("flags", []):
        if flag in FLAG_TEXT:
            fl.add("notes", FLAG_TEXT[flag], kind="note")
    commodity = commodity_note(state.get("sector"))
    if commodity:
        fl.add("notes", commodity, kind="note")
    return fl.facts


def facts_text(facts: list[dict]) -> str:
    """The facts as the model sees them, grouped under the block titles in order."""
    out = []
    for block in BLOCKS:
        rows = [f for f in facts if f["block"] == block]
        if rows:
            out.append(BLOCK_TITLES[block])
            out += [f"[{f['id']}] {f['text']}" for f in rows]
            out.append("")
    return "\n".join(out).strip()
