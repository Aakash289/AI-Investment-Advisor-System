"""
reporting/report_data.py: the one data object the PDF is drawn from (design 14.14).

What this file does:
    build() reads the final state and returns a plain dictionary with everything the
    report shows, every value already formatted as text by one shared function (show()),
    so the PDF cannot show a number differently from the analysis. Nothing in the
    renderer works a number out or formats one.

    Sources: the reasoning cites facts as [F3]; here each fact's record IDs become
    numbered sources ([1], [2]...), in the order they are first cited, and the tags in
    the text are replaced by those numbers. Every number in the text therefore points to
    an entry in the Sources section, with its outlet, date, link or filing number.

    A value that is missing is never left blank: show() prints "Not available", and the
    sections that cannot be drawn (no projection, no peers) say why.

    The object is saved next to the PDF as JSON, so the report can be checked or drawn
    again later from exactly what the person was shown.
"""

from __future__ import annotations

import re
from datetime import date

from invest_agent.analysis.projection import commodity_note
from invest_agent.analysis.scorecard import WEAK_PEERS_REASON, near_line, reliability_reason
from invest_agent.analysis.target import describe as describe_target
from invest_agent.analysis.target import target_lines
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.compliance.disclaimer import VERDICT_WORDS, situation_lines
from invest_agent.compliance.disclaimer import config as disclaimer_config
from invest_agent.fx.convert import format_large, format_money
from invest_agent.profile.budget_parser import format_amount
from invest_agent.verification.record_index import RecordIndex

NA = "Not available"
SUB_ORDER = ("business_quality", "valuation", "macro_fit", "news", "forecast")
SUB_PREFIX = {"business_quality": ("growth", "profitability", "margin_trend", "balance_sheet", "cash_generation"),
              "valuation": ("valuation_",), "macro_fit": ("macro_",), "news": ("news",), "forecast": ("forecast",)}
TAG = re.compile(r"\[(F\d+(?:\s*,\s*F\d+)*)\]")


def show(value, kind: str = "plain", currency: str | None = None, decimals: int = 1) -> str:
    """The one formatter. kinds: pct (0.123 -> 12.3%), spct (+12.3%), times, money, large, plain, int."""
    if value is None or (isinstance(value, float) and value != value):
        return NA
    if kind == "pct":
        return f"{value * 100:.{decimals}f}%"
    if kind == "spct":                                   # a change, with its sign
        return f"{value * 100:+.{decimals}f}%"
    if kind == "times":
        return f"{value:.{decimals}f}x"
    if kind in ("money", "large"):
        fmt = format_money if kind == "money" else format_large
        text = fmt(abs(value), currency or "USD")        # the shared formatters expect positive amounts
        return f"-{text}" if value < 0 else text
    if kind == "int":
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:,.{decimals}f}"
    return str(value)


def all_records(state: dict) -> dict[str, dict]:
    out = {}
    for key in ("verified_records", "news_records", "macro_records", "geo_sector_records", "fx_records",
                "projection_records"):
        for r in state.get(key) or []:
            out[r["record_id"]] = r
    return out


class Sources:
    """Numbers sources in the order they are first cited."""

    def __init__(self, facts: list[dict], records: dict[str, dict]):
        self.facts = {f["id"]: f for f in facts}
        self.records = records
        self.order: list[str] = []          # source keys
        self.entries: dict[str, dict] = {}

    def _add(self, key: str, entry: dict) -> int:
        if key not in self.entries:
            self.entries[key] = entry
            self.order.append(key)
        return self.order.index(key) + 1

    def numbers_for_fact(self, fact_id: str) -> list[int]:
        """The source numbers behind one fact. A record the analysis holds becomes its own
        source; an ID with no record behind it (a figure the analysis worked out, such as a
        peer median) is listed with the fact's own wording, so the reader sees what it is."""
        fact = self.facts.get(fact_id)
        if not fact:
            return []
        nums = []
        found = [rid for rid in fact["record_ids"] if rid in self.records]
        for rid in found:
            r = self.records[rid]
            nums.append(self._add(rid, {
                "id": rid, "source": r.get("source") or rid.split(":")[0],
                "as_of": r.get("as_of") or r.get("period") or "", "ref": r.get("source_ref") or "",
                "accession": r.get("accession_number") or "", "projected": "projected" in (r.get("flags") or []),
                "field": (r.get("field") or "").replace("_", " ")}))
        if not found:                       # one entry for the fact itself, in its own words
            nums.append(self._add(f"analysis:{fact_id}", {
                "id": f"analysis:{fact_id}", "source": "worked out in this analysis", "as_of": "", "ref": "",
                "accession": "", "projected": fact.get("kind") == "projection", "field": fact["text"]}))
        return sorted(set(nums))

    def retag(self, text: str) -> str:
        """[F3, F7] -> [1, 2, 5]."""
        def sub(m):
            nums = sorted({n for f in m.group(1).split(",") for n in self.numbers_for_fact(f.strip())})
            return "[" + ", ".join(str(n) for n in nums) + "]" if nums else ""
        return TAG.sub(sub, text)

    def listing(self) -> list[dict]:
        return [{"n": i + 1, **self.entries[k]} for i, k in enumerate(self.order)]


def _price_note(chart: dict, idx: RecordIndex) -> str | None:
    """Why the 3- or 5-year figures are missing: prices start later (a listing within the
    last five years, as for Star Health in December 2021)."""
    dates = chart.get("dates") or []
    if not dates:
        return None
    start = date.fromisoformat(dates[0]).strftime("%B %Y")
    if idx.value("return_5y") is None:
        held = f"about {chart['years']:g} years" if chart.get("years") else "less than five years"
        return (f"Prices start in {start} ({held}), usually because the shares listed then, so the 5-year "
                f"figures are not available.")
    return None


def _history(idx: RecordIndex, currency: str, bank: bool) -> dict:
    years = sorted(idx.years("revenue"))[-6:]
    rows = [("Revenue", "revenue", "large"), ("Net profit", "net_income", "large"),
            ("Operating margin", "operating_margin", "pct"), ("Net margin", "net_margin", "pct"),
            ("Free cash flow", "free_cash_flow", "large"),
            ("ROE" if bank else "ROIC", "roe" if bank else "roic", "pct")]
    if bank:
        # Banks and insurers have no free cash flow in the usual sense (design 14.3), so the
        # row would only ever say "Not available".
        rows = [r for r in rows if r[1] != "free_cash_flow"]
    table = [[label] + [show(idx.value(field, y), kind, currency) for y in years] for label, field, kind in rows]
    series = {field: [idx.value(field, y) for y in years] for _, field, _ in rows}
    series["gross_margin"] = [idx.value("gross_margin", y) for y in years]
    return {"years": [f"FY{y}" for y in years], "table": table, "series": series}


def _lines_text() -> str:
    """The verdict lines as the scorecard config sets them, e.g. "+0.75 or above is
    favourable, -0.5 or below unfavourable"."""
    from invest_agent.analysis.scorecard import config as card_config
    t = card_config()["verdict_thresholds"]
    return (f"{t['favourable_above']:+g} or above is favourable, "
            f"{t['unfavourable_below']:+g} or below unfavourable")


def projection_notes(state: dict, a: dict) -> list[str]:
    """Plain-language notes on how the base growth was chosen, and its limits."""
    notes = []
    if a.get("recent_years"):
        notes.append(f"Base growth is the average of the last 3 usual years ({a['recent_years'][0]} to "
                     f"{a['recent_years'][-1]}); the low and high cases add or take away the usual year-to-year "
                     f"swing ({show(a.get('growth_sd'), 'pct')}).")
    proj_flags = (state.get("projection") or {}).get("flags", [])
    if "growth_capped_base" in proj_flags or "growth_floored_base" in proj_flags:
        capped = "growth_capped_base" in proj_flags
        limit = a.get("growth_cap" if capped else "growth_floor")
        notes.append(f"Growth over those years averaged {show(a.get('trend_growth'), 'pct')} a year, "
                     f"{'above the cap' if capped else 'below the floor'} of {show(limit, 'pct')}, so the base case "
                     f"uses {show(limit, 'pct')}. Growth that {'fast' if capped else 'steep a fall'} says little about "
                     f"next year, so the {'low' if capped else 'high'} case is kept at least "
                     f"{(a.get('growth_min_swing') or 0) * 100:.0f} points away.")
    for year, rate in (a.get("left_out_years") or {}).items():
        notes.append(f"{year}'s revenue growth of {show(rate, 'pct')} was far from the other years (a rebound, "
                     f"a boom or an acquisition) and was left out.")
    for year, m in (a.get("margin_left_out_years") or {}).items():
        notes.append(f"{year}'s net margin of {show(m, 'pct')} was far from the other years (a pandemic loss or a "
                     f"one-off gain) and was left out of the low and high cases.")
    proj = state.get("projection") or {}
    if "latest_year_unusual" in proj.get("flags", []):
        notes.append("The latest year's revenue growth is far from the earlier years, which can mean a change in "
                     "how revenue is counted or a step change in the business. The projection rests on the older "
                     "years, so it is shown for reference only and no fair value is given.")
    c = commodity_note(state.get("sector"))
    if c:
        notes.append(c)
    return notes


def build(state: dict, today: date | None = None) -> dict:
    today = today or date.today()
    currency = state.get("listing_currency") or "USD"
    profile = state.get("user_profile") or {}
    records = all_records(state)
    idx = RecordIndex(state.get("verified_records") or [])
    src = Sources(state.get("facts") or [], records)
    card = state.get("scorecard") or {}
    sizing = state.get("sizing") or {}
    dcfg = disclaimer_config()

    reasoning = [src.retag(p["text"]) for p in state.get("reasoning") or []]
    risks = [src.retag(r["text"]) for r in state.get("key_risks") or []]
    changes = [src.retag(w) for w in state.get("what_would_change_the_view") or []]

    horizon = profile.get("horizon_years")
    data = {
        "title": f"{state.get('company_name')} ({state.get('ticker')}, {state.get('exchange')})",
        "date": today.isoformat(),
        "profile": (f"Budget {format_amount(profile['budget_amount'], profile.get('budget_currency'))}"
                    if profile.get("budget_amount") else "No budget given")
                   + f", {profile.get('risk_tolerance') or 'medium'} risk tolerance, "
                   + (f"{horizon:g}-year horizon" if isinstance(horizon, (int, float)) else "horizon not given")
                   + f", goal {str(profile.get('goal') or 'not given').replace('_', ' ')}"
                   + (f", target {describe_target(profile)['text']}" if describe_target(profile) else ""),
        "disclaimer": {"short": dcfg.get("short"), "full": dcfg.get("full"), "version": dcfg.get("version")},
        "verdict": VERDICT_WORDS.get(state.get("verdict"), state.get("verdict") or NA),
        "confidence": state.get("confidence") or NA,
        "verdict_change": state.get("verdict_change_reason") or "",
        "near_line": None if state.get("verdict_change_reason") else near_line(state.get("scorecard")),
        "situations": situation_lines(state),
        "reasoning": reasoning, "key_risks": risks, "what_would_change": changes,
    }

    # Amount.
    if sizing.get("status") == "ok":
        cur, stock = sizing["currency"], sizing["stock_currency"]
        # The share of a full position that the amount shown actually is (after whole shares).
        shown = sizing["amount"] if sizing["shares"] else sizing["suggested"]
        of_full = (f" ({shown / sizing['full_position'] * 100:.0f}% of a full position of "
                   f"{show(sizing['full_position'], 'money', cur)})" if sizing.get("full_position") else "")
        if sizing["shares"]:
            data["amount"] = (f"About {show(sizing['amount'], 'money', cur)}: {sizing['shares']} shares at "
                              f"{show(sizing['price'], 'money', stock)}{of_full}")
        else:
            data["amount"] = (f"About {show(sizing['suggested'], 'money', cur)}, less than the price of one share "
                              f"({show(sizing['price'], 'money', stock)}){of_full}")
    elif sizing.get("status") == "zero":
        data["amount"] = "None suggested"
    else:
        data["amount"] = "Not given: " + (sizing.get("reason") or "no amount was worked out")
    data["amount_notes"] = [n for n in sizing.get("notes", []) if "do not already own" not in n] + \
        [sizing[k] for k in ("overexposure_note", "liquidity_note") if sizing.get(k)]
    data["sizing_steps"] = [s["text"] for s in sizing.get("steps", [])]
    data["target"] = target_lines(state)
    fx = state.get("fx") or {}
    data["currency_note"] = fx.get("risk_note") if fx.get("needed") else None

    # Share price: up to five years, with the market index for comparison.
    chart = state.get("price_chart") or {}
    bench_names = {"^NSEI": "Nifty 50", "^GSPC": "S&P 500"}
    price_rows = [["Current price", show(idx.value("current_price"), "money", currency)],
                  ["52-week range", f"{show(idx.value('low_52w'), 'money', currency)} to "
                                    f"{show(idx.value('high_52w'), 'money', currency)}"],
                  ["Change over 1 year", show(idx.value("return_12m"), "spct")],
                  ["Change over 3 years", show(idx.value("return_3y"), "spct")],
                  ["Change over 5 years", show(idx.value("return_5y"), "spct")],
                  ["Growth a year over 5 years", show(idx.value("price_cagr_5y"), "spct")],
                  [f"{bench_names.get(chart.get('benchmark_symbol'), 'Market index')} growth a year over 5 years",
                   show(idx.value("benchmark_cagr_5y"), "spct")],
                  ["Worst fall from a peak (5 years)", show(idx.value("max_drawdown_5y"), "spct")],
                  ["Volatility (1 year)", show(idx.value("volatility_1y"), "pct")],
                  ["Beta (against the market)", show(idx.value("beta"), decimals=2)]]
    data["price"] = {"rows": price_rows, "chart": chart if chart.get("stock") else None,
                     "benchmark_name": bench_names.get(chart.get("benchmark_symbol"), chart.get("benchmark_symbol")),
                     "note": _price_note(chart, idx)}

    # The business.
    data["history"] = _history(idx, currency, bool(state.get("bank_mode")))

    # Valuation.
    val = state.get("valuation") or {}
    peers = val.get("peers") or {}
    names = {"pe": "P/E", "pb": "P/B", "ev_ebitda": "EV/EBITDA", "ps": "P/S"}
    rows = []
    for m, label in names.items():
        comp = (peers.get("comparisons") or {}).get(m) or {}
        own = comp.get("company", (val.get("multiples") or {}).get(m))
        prem = comp.get("premium")
        rows.append([label, show(own), show(comp.get("median")),
                     NA if prem is None else f"{abs(prem) * 100:.0f}% {'dearer' if prem > 0 else 'cheaper'}"])
    data["valuation"] = {"rows": rows, "peers": ", ".join(peers.get("peers") or []) or NA,
                         "left_out": ", ".join(f"{e['symbol']} ({e['reason']})" for e in peers.get("excluded", [])),
                         "basis": peers.get("basis") or "",
                         "weak": bool(card.get("low_reliability"))
                         and reliability_reason(card, "valuation") == WEAK_PEERS_REASON,
                         "fallback_note": None}
    why = reliability_reason(card, "valuation")
    if why and why != WEAK_PEERS_REASON:
        data["valuation"]["fallback_note"] = (
            f"Low reliability: {why}. The valuation sub-score uses this comparison at half its usual weight"
            + (", and so does the fair value built on it." if reliability_reason(card, "forecast") else "."))

    # Projection.
    proj, fv = state.get("projection") or {}, state.get("fair_value") or {}
    tagged = [r for r in state.get("projection_records") or [] if "projected" in (r.get("flags") or [])]
    if proj.get("status") == "ok" and tagged:
        a, y1, y2 = proj["assumptions"], proj["years"][0], proj["years"][-1]
        cases = ("low", "base", "high")
        data["projection"] = {
            "assumptions": [f"Revenue growth a year: low {show(a['growth']['low'], 'pct')}, base "
                            f"{show(a['growth']['base'], 'pct')}, high {show(a['growth']['high'], 'pct')}",
                            f"Net margin: low {show(a['net_margin']['low'], 'pct')}, base "
                            f"{show(a['net_margin']['base'], 'pct')}, high {show(a['net_margin']['high'], 'pct')}",
                            "Shares held at today's count, so buybacks are ignored"] + projection_notes(state, a),
            "table": [["Figure", "Low", "Base", "High"],
                      [f"Revenue FY{y1}"] + [show(proj["cases"][c][y1]["revenue"], "large", currency) for c in cases],
                      [f"EPS FY{y1}"] + [show(proj["cases"][c][y1].get("eps"), "money", currency) for c in cases],
                      [f"Revenue FY{y2} (trajectory)"] + [show(proj["cases"][c][y2]["revenue"], "large", currency)
                                                         for c in cases]],
            "flags": proj.get("flags", []),
        }
        if fv.get("status") == "ok":
            r = fv["range"]
            data["projection"]["fair_value"] = {
                "text": (f"{show(r.get('low'), 'money', currency)} to {show(r.get('high'), 'money', currency)}, base "
                         f"{show(r['base'], 'money', currency)}, against today's price of "
                         f"{show(fv.get('price'), 'money', currency)}"),
                "values": {"low": r.get("low"), "base": r["base"], "high": r.get("high"), "price": fv.get("price")},
                "low_reliability": "forecast" in card.get("low_reliability", [])}
        else:
            data["projection"]["no_fair_value"] = f"No fair value, because {fv.get('reason') or 'it could not be worked out'}"
    else:
        data["projection"] = None
        data["no_projection"] = "No projection, because " + (", ".join(proj.get("flags", [])) or "there was too little history")

    # Economy.
    geo, panel = state.get("geo_exposure") or {}, (state.get("macro_panel") or {}).get("countries") or {}
    data["economy"] = [[(panel.get(code) or {}).get("name", "Rest of world" if code == "ROW" else code), show(share, "pct"),
                        ", ".join(f"{k.replace('_', ' ')} {v.replace('_', ' ')}"
                                  for k, v in ((panel.get(code) or {}).get("regimes") or {}).items()) or "Not scored"]
                       for code, share in sorted((geo.get("weights") or {}).items(), key=lambda kv: -kv[1])]

    # News.
    news = state.get("news") or {}
    by_id = {i["id"]: i for i in news.get("items", [])}
    data["news"] = [{"date": i.get("date"), "outlet": i.get("outlet"), "title": i.get("title"), "url": i.get("url"),
                     "label": f"{i.get('sentiment')}, {i.get('materiality')}"}
                    for i in (by_id.get(n) for n in (news.get("summary") or {}).get("top_ids", [])) if i]
    data["filings"] = [f.get("title") if str(f.get("title", "")).startswith("8-K") else
                       f"8-K filed {f.get('date')}: {f.get('title')}" for f in news.get("filings", [])[:5]]

    # Data notes and the working.
    notes = list(state.get("data_notes") or [])
    line = (state.get("completeness_detail") or {}).get("user_line")
    if line:
        notes.append(line)
    data["data_notes"] = notes or ["No data quality issues were found."]
    subs = state.get("sub_scores") or {}
    weights = card.get("weights_used") or {}
    text = load_yaml("reporting/report_text.yaml")
    low = set(card.get("low_reliability") or [])
    findings = state.get("findings") or []
    data["scorecard"], data["subscores"] = [], []
    for k in SUB_ORDER:
        value, w = subs.get(k), weights.get(k)
        score = f"{float(value):+.2f}" if value is not None else "Missing"
        contribution = f"{float(value) * w:+.2f}" if value is not None and w is not None else "0.00"
        data["scorecard"].append([text["subscores"][k]["title"] + (" (low reliability)" if k in low else ""), score,
                                  show(w, "pct", decimals=0) if w is not None else "0%", contribution])
        parts = [f["text"] + (f" (scores {f['score']:+g})" if f.get("score") is not None else "")
                 for f in findings if f["component"].startswith(SUB_PREFIX[k])]
        missing = ((state.get("analysis_detail") or {}).get(k) or {}).get("missing") or []
        if value is None:
            parts.append("Missing: " + (", ".join(missing) if missing else "not enough data")
                         + ". Its weight was shared among the other sub-scores.")
        elif missing:
            parts.append("Not available, weight shared among the other parts: " + ", ".join(missing).replace("_", " "))
        data["subscores"].append({**text["subscores"][k], "key": k, "score": score,
                                  "weight": show(w, "pct", decimals=0) if w is not None else "0%",
                                  "low_reliability": k in low, "this_company": parts})
    data["scorecard_total"] = (f"Weighted score {card['score']:+.2f}, which gives a {VERDICT_WORDS.get(card.get('raw_verdict'), '').lower()} "
                               f"result before any caps ({_lines_text()})"
                               if card.get("score") is not None else NA)
    data["confidence_reasons"] = list((card.get("confidence") or {}).get("reasons", []))
    # The guardrail can lower confidence after the scorecard (Star Health: the scorecard's
    # reason read as medium, the report showed low with no word why).
    if "confidence_lowered_by_guardrail" in (state.get("guardrail_flags") or []):
        n = len(state.get("removed_sentences") or [])
        data["confidence_reasons"].append(
            f"lowered one step to {state.get('confidence')}: the number check removed {n} "
            f"sentence{'s' if n != 1 else ''} from the written reasoning that did not match {'their' if n != 1 else 'its'} "
            f"sources")
    data["reading"] = text["reading"]
    data["backtest_note"] = text.get("backtest_note")
    data["section_intros"] = text["sections"]
    data["glossary"] = [list(row) for row in text["glossary"]]

    # At a glance, for the cover page.
    fv_line = NA
    if fv.get("status") == "ok":
        r = fv["range"]
        fv_line = (f"{show(r.get('low'), 'money', currency)} to {show(r.get('high'), 'money', currency)} "
                   f"(projection; today {show(fv.get('price'), 'money', currency)})")
    elif fv.get("reason"):
        fv_line = "None: " + fv["reason"]
    completeness = (state.get("completeness_detail") or {}).get("score", state.get("completeness_score"))
    data["glance"] = [["Verdict", data["verdict"]], ["Confidence", str(data["confidence"]).capitalize()],
                      ["Suggested amount", data["amount"]],
                      ["Weighted score (-2 to +2)", f"{card['score']:+.2f}" if card.get("score") is not None else NA],
                      ["Fair value a year out", fv_line],
                      ["Data completeness", show(completeness, "pct", decimals=0) if completeness is not None else NA]]
    data["config_version"] = card.get("config_version")
    data["sources"] = src.listing()
    return data
