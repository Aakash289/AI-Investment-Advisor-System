"""
graph/nodes.py: the small plain-code nodes that join the agents together.

What this file does:
    The agents (entity resolution, profile intake, the retrieval agents) live in
    agents/. This file holds the glue nodes that sit between them in the graph:

      input_gate      runs once both input agents have finished. It applies the
                      currency default to the budget, then decides whether the request
                      can go on or must stop and ask the user something (which company,
                      the missing profile details, or a budget to confirm), or stop
                      because the company is not covered in version 1.

      route_after_gate  the rule LangGraph uses after the gate: stop, or start every
                      retrieval agent at once (RETRIEVAL_AGENTS).

      preview_output  the developer view of everything gathered and worked out, shown
                      with --detail (since step 12; the user's answer comes from
                      compliance/disclaimer.py).

    The FX step itself lives in fx/step.py; preview_output shows its result (the rate,
    the budget in the stock's currency and the currency risk note). Later steps add the
    wrappers for verification, analysis, guardrail, sizing, report and compliance.
"""

from __future__ import annotations

from langgraph.graph import END

from invest_agent.analysis.scorecard import reliability_reason
from invest_agent.fx.convert import describe_rate, format_large, format_money
from invest_agent.graph.state import GraphState
from invest_agent.profile.budget_parser import format_amount
from invest_agent.profile.followup import apply_currency_default, combined_question


def input_gate(state: GraphState) -> dict:
    """Join point after Entity Resolution and Profile Intake (diagram nodes B3, BX)."""
    profile = apply_currency_default(dict(state.get("user_profile") or {}), state.get("listing_currency"))
    update: dict = {"user_profile": profile}

    # Not covered in version 1: stop with the reason (diagram node NC).
    if state.get("entity_status") == "not_covered":
        name = state.get("company_name") or "This company"
        update["verdict"] = "not_covered"
        update["final_response"] = f"{name} is not covered in version 1 because {state.get('not_covered_reason')}."
        return update

    questions = []
    if state.get("entity_status") in ("ask_user", "not_found"):
        questions.append(state.get("entity_question") or "Which company would you like me to look at?")

    if state.get("followup_question"):
        if state.get("missing_profile_fields") and not state.get("budget_needs_confirmation"):
            # Rebuild the question now that the company name is known (the two input
            # agents ran in parallel, so intake did not have it yet).
            questions.append(combined_question(state["missing_profile_fields"], state.get("company_name")))
        else:
            questions.append(state["followup_question"])

    update["final_response"] = "\n\n".join(questions)
    return update


# The retrieval agents started together once the gate lets a request through.
RETRIEVAL_AGENTS = ["technical_price", "fundamentals", "macro", "news", "geo_sector"]


def route_after_gate(state: GraphState):
    """END stops this turn with a question; a company not covered goes to compliance,
    which adds the disclaimer; otherwise the list of retrieval agents to run in parallel."""
    if state.get("verdict") == "not_covered":
        return "compliance"
    return END if state.get("final_response") else list(RETRIEVAL_AGENTS)


def route_after_verification(state: GraphState):
    """END when verification found the company too small for version 1 (it has already
    written the reply); otherwise on to the next step."""
    return "compliance" if state.get("verdict") == "not_covered" else "analysis"


def _pct(value, signed: bool = True) -> str:
    """0.089 -> "+8.9%" for changes; signed=False -> "8.9%" for levels such as volatility."""
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"


def preview_output(state: GraphState) -> dict:
    """The developer view: everything gathered and worked out, shown with --detail. The
    answer the user reads is written by the compliance node after this."""
    profile = state.get("user_profile") or {}
    figures = {r["field"]: r for r in state.get("price_records", [])}

    def fig(name):
        record = figures.get(name)
        return None if record is None else record["value"]

    lines = [f"Company: {state.get('company_name')} ({state.get('ticker')}, {state.get('exchange')}, {state.get('listing_currency')})"]

    if profile.get("budget_amount"):
        budget = format_amount(profile["budget_amount"], profile.get("budget_currency"))
        if profile.get("budget_currency_assumed"):
            budget += " (currency assumed)"
    else:
        budget = "not given, so no amount will be recommended"
    lines.append(f"Budget: {budget}{_budget_in_stock_currency(state)}")
    lines.append(f"Profile: risk {profile.get('risk_tolerance')}, horizon {profile.get('horizon_years')} years, "
                 f"goal {profile.get('goal')}")
    if profile.get("assumed_fields"):
        lines.append(f"Assumed: {', '.join(profile['assumed_fields'])}")

    if "current_price" in figures:
        currency = state.get("listing_currency")
        price = fig("current_price")
        price_text = f"₹{price:,.2f}" if currency == "INR" else f"${price:,.2f}"
        source = figures["current_price"]["source"] if "current_price" in figures else "unknown"
        lines += [
            f"Price: {price_text} as of {figures['current_price']['as_of']} (source: {source})",
            f"52-week range position: {fig('position_in_52w_range')}",
            f"Returns: 1m {_pct(fig('return_1m'))}, 3m {_pct(fig('return_3m'))}, "
            f"6m {_pct(fig('return_6m'))}, 12m {_pct(fig('return_12m'))}",
            f"Volatility (1 year): {_pct(fig('volatility_1y'), signed=False)}   Beta: {fig('beta')}   "
            f"Max drawdown (2 years): {_pct(fig('max_drawdown_2y'))}",
            f"Thinly traded: {fig('thinly_traded')}",
        ]
    else:
        lines.append("Price data: not available")

    lines += _fundamentals_lines(state)
    lines += _macro_lines(state)
    lines += _news_lines(state)
    lines += _geo_lines(state)
    lines += _currency_lines(state)
    lines += _verification_lines(state)
    lines += _projection_lines(state)
    lines += _score_lines(state)
    lines += _scorecard_lines(state)
    lines += _synthesis_lines(state)
    lines += _sizing_lines(state)
    lines.append("\n(Developer view.)")
    return {"preview_response": "\n".join(lines)}


def _fundamentals_lines(state: GraphState) -> list[str]:
    """The latest year's key figures from the Fundamentals Agent, for the preview."""
    records = state.get("fundamentals_records", [])
    if not records:
        return ["Fundamentals: not available"]
    # The latest year that has revenue (a share count dated after the year end must
    # not make a year with no statements look like the latest one).
    revenue_years = [r["fiscal_year"] for r in records if r["field"] == "revenue"]
    latest_year = max(revenue_years) if revenue_years else max(r.get("fiscal_year", 0) for r in records)
    latest = {r["field"]: r for r in records if r.get("fiscal_year") == latest_year}
    years = len({r.get("fiscal_year") for r in records if r["field"] == "revenue"})
    currency = state.get("listing_currency")

    def money(name):
        r = latest.get(name)
        if r is None:
            return "n/a"
        return format_large(r["value"], currency)

    def pct(name):
        r = latest.get(name)
        return "n/a" if r is None else f"{r['value'] * 100:.1f}%"

    def num(name):
        r = latest.get(name)
        return "n/a" if r is None else f"{r['value']:.2f}"

    source = latest["revenue"]["source"] if "revenue" in latest else "unknown"
    cagr = next((r for r in records if r["field"] == "revenue_cagr"), None)
    growth = "n/a" if cagr is None else f"{cagr['value'] * 100:.1f}% a year"
    return [
        f"Fundamentals FY{latest_year} (source: {source}, {years} years of revenue"
        f"{', bank mode' if state.get('bank_mode') else ''}):",
        f"  Revenue {money('revenue')}, net profit {money('net_income')}, "
        f"free cash flow {money('free_cash_flow')}",
        f"  Margins: operating {pct('operating_margin')}, net {pct('net_margin')}   "
        f"ROE {pct('roe')}   ROIC {pct('roic')}",
        f"  Debt to equity {num('debt_to_equity')}   Net debt to EBITDA {num('net_debt_to_ebitda')}   "
        f"Interest coverage {num('interest_coverage')}",
        f"  Revenue growth {growth}",
    ]


def _budget_in_stock_currency(state: GraphState) -> str:
    """The budget in the stock's currency, e.g. (about $2,079.00 in USD), when the FX step converted it."""
    converted = (state.get("fx") or {}).get("budget_converted")
    if not converted:
        return ""
    c = converted["converted"]
    return f" (about {format_money(c['amount'], c['currency'])} in {c['currency']}, indicative)"


def _currency_lines(state: GraphState) -> list[str]:
    """The rate used, its warnings and the currency risk note, when currencies differ."""
    fx = state.get("fx") or {}
    if not fx.get("needed"):
        return []
    lines = ["Currency:"]
    rate = fx.get("rate") or {}
    if rate.get("rate") is not None:
        lines.append(f"  Rate: {describe_rate(rate)}")
        check = rate.get("cross_check") or {}
        if check.get("status") in ("agree", "disagree"):
            lines.append(f"  Cross-check with FRED on {check['date']}: {check['frankfurter_rate']:.2f} vs "
                         f"{check['fred_rate']:.2f} ({check['gap'] * 100:.2f}% apart, {check['status']})")
    warnings = [f for f in fx.get("flags", []) if f in WARNING_FLAGS]
    if warnings:
        lines.append(f"  Warnings: {', '.join(warnings)}")
    if fx.get("risk_note"):
        lines.append(f"  {fx['risk_note']}")
    lines += [f"  {note}" for note in fx.get("notes", [])]
    return lines


# Flags worth showing to the user; the others (from_cache, fbil_rate_behind) are routine.
WARNING_FLAGS = {"fx_rate_stale", "fx_sources_disagree", "fx_lagged_fred_rate", "fx_unofficial_rate",
                 "fx_unavailable", "fx_server_failed", "fx_series_unavailable"}


MULTIPLE_NAMES = {"pe": "P/E", "pb": "P/B", "ev_ebitda": "EV/EBITDA", "ps": "P/S"}


def _verification_lines(state: GraphState) -> list[str]:
    """Completeness, checks, the company's own multiples and the data notes."""
    detail = state.get("completeness_detail")
    if not detail:
        return []
    lines = ["Checks:"]
    gate = {"proceed": "enough to proceed", "lower_confidence": "confidence lowered one step",
            "withhold_amount": "amount withheld"}[detail["decision"]]
    lines.append(f"  Completeness {detail['score'] * 100:.0f}% ({gate})"
                 + (f": {detail['reason']}" if detail.get("reason") else ""))
    lines.append(f"  Sanity findings: {len(state.get('sanity_flags') or [])}   Conflicts: {len(state.get('conflicts') or [])}")
    if detail.get("user_line"):
        lines.append(f"  {detail['user_line']}")

    val = state.get("valuation") or {}
    multiples, currency = val.get("multiples") or {}, val.get("currency")
    if multiples:
        parts = [f"market value {format_large(multiples['market_value'], currency)}"] if "market_value" in multiples else []
        parts += [f"{label} {multiples[k]:.1f}" for k, label in MULTIPLE_NAMES.items() if k in multiples]
        if "dividend_yield" in multiples:
            parts.append(f"dividend yield {multiples['dividend_yield'] * 100:.2f}%")
        lines.append(f"Valuation FY{val.get('year')} (own multiples): " + ", ".join(parts))
        for name, reason in (val.get("not_meaningful") or {}).items():
            lines.append(f"  {MULTIPLE_NAMES.get(name, name)}: not meaningful, {reason}")
        lines += _peer_lines(val.get("peers") or {})
    elif val.get("note"):
        lines.append(f"Valuation: {val['note']}")
    lines += [f"  Note: {n}" for n in state.get("data_notes") or []]
    return lines


def _peer_lines(peers: dict) -> list[str]:
    """Each multiple against the peer median, with the premium or discount."""
    if not peers:
        return []
    if peers.get("status") == "no_peers":
        return [f"  Peers: {peers.get('note')}"]
    shown = ", ".join(peers.get("peers", [])[:10]) + (" ..." if peers.get("peer_count", 0) > 10 else "")
    lines = [f"  Peers ({peers.get('peer_count')}): {shown}", f"  Basis: {peers.get('basis')}"]
    for m, c in (peers.get("comparisons") or {}).items():
        mine = "n/a" if c.get("company") is None else f"{c['company']:.1f}"
        if c.get("premium") is None:
            side = "no comparison"
        else:
            side = f"{abs(c['premium']) * 100:.0f}% {'dearer' if c['premium'] > 0 else 'cheaper'} than peers"
        lines.append(f"  {MULTIPLE_NAMES.get(m, m)}: company {mine} vs peer median {c['median']:.1f} "
                     f"({c['peers_used']} peers), {side}")
    for m, reason in (peers.get("not_computed") or {}).items():
        lines.append(f"  {MULTIPLE_NAMES.get(m, m)}: no peer median, {reason}")
    if peers.get("excluded"):
        lines.append("  Left out: " + ", ".join(f"{e['symbol']} ({e['reason']})" for e in peers["excluded"]))
    if peers.get("missing_figures"):
        lines.append("  Peers missing figures: " + "; ".join(f"{s}: {', '.join(m)}" for s, m in peers["missing_figures"].items()))
    return lines


def _macro_lines(state: GraphState) -> list[str]:
    """The home economy in a little detail, and one line of labels per other country."""
    panel = state.get("macro_panel") or {}
    countries = panel.get("countries") or {}
    if not countries:
        return ["Economy: not available"] if panel else []
    home = panel.get("home", "US")

    def ind(code, name, fmt="{:.2f}%"):
        i = (countries.get(code, {}).get("indicators") or {}).get(name)
        if not i or i.get("value") is None:
            return "n/a"
        text = fmt.format(i["value"]) + f" ({i.get('date')}"
        text += ", estimate" if i.get("estimate") else ""
        text += ", stale" if i.get("stale") else ""
        return text + ")"

    def labels(code):
        r = countries.get(code, {}).get("regimes") or {}
        return ", ".join(f"{k.replace('_', ' ')} {v.replace('_', ' ')}" for k, v in r.items()) or "no labels"

    c = countries.get(home, {})
    lines = [f"Economy ({c.get('name', home)}, home market):",
             f"  Policy rate {ind(home, 'policy_rate')}   Inflation {ind(home, 'inflation')}   GDP growth {ind(home, 'gdp_growth')}"]
    if home == "US":
        lines.append(f"  10-year yield {ind('US', 'bond_10y')}   Yield curve {ind('US', 'yield_curve', '{:+.2f}')}   "
                     f"Unemployment {ind('US', 'unemployment')}   High-yield spread {ind('US', 'credit_spread', '{:.2f}')}")
    else:
        rupee = (countries.get("IN", {}).get("indicators") or {}).get("currency")
        if rupee and rupee.get("change_12m_pct") is not None:
            lines.append(f"  Rupee per dollar {rupee['value']:.2f} ({rupee['date']}), "
                         f"{rupee['change_12m_pct']:+.1f}% over 12 months")
    lines.append(f"  Regimes: {labels(home)}")
    others = [f"{v.get('name', k)}: {labels(k)}" for k, v in countries.items() if k != home]
    lines.append("  Panel: " + " | ".join(others))
    return lines


def _news_lines(state: GraphState) -> list[str]:
    """Net sentiment, the top items with their outlets and dates, filings and warnings."""
    news = state.get("news")
    if news is None:
        return []
    summary = news.get("summary") or {}
    items = {i["id"]: i for i in news.get("items", []) if "id" in i}
    net = summary.get("net_sentiment")
    counts = summary.get("counts") or {}
    head = (f"net sentiment {net:+.2f} from {summary.get('relevant_count')} relevant items "
            f"({counts.get('positive', 0)} positive, {counts.get('neutral', 0)} neutral, {counts.get('negative', 0)} negative)"
            if net is not None else "no labelled items")
    lines = [f"News (last month): {head}"]
    for item_id in summary.get("top_ids", []):
        i = items[item_id]
        # Round brackets: the terminal printer (rich) treats square brackets as formatting codes and hides them.
        lines.append(f"  ({i.get('sentiment')}, {i.get('materiality')}) {i.get('date')} {i.get('outlet')}: {i.get('title')}")
    for f in news.get("filings", [])[:5]:
        lines.append(f"  {f['title']}" + ("  (SERIOUS)" if f.get("serious") else ""))
    if summary.get("serious_negative_ids"):
        lines.append("  Serious negative news: " + ", ".join(items[i]["title"] for i in summary["serious_negative_ids"]))
    spike = news.get("spike") or {}
    if spike.get("status") == "spike":
        lines.append(f"  Coverage spike: {spike.get('ratio')} times the usual level")
    warn = [f for f in news.get("flags", []) if f != "from_cache"]
    if warn:
        lines.append(f"  Notes: {', '.join(warn)}")
    return lines


PANEL_NAMES = {"US": "US", "IN": "India", "CN": "China", "EA": "Eurozone", "JP": "Japan", "GB": "UK", "ROW": "rest of world"}


def _geo_lines(state: GraphState) -> list[str]:
    """Revenue by economy, with its source and check, and the sector."""
    geo = state.get("geo_exposure")
    if not geo:
        return []
    shares = ", ".join(f"{PANEL_NAMES.get(k, k)} {v * 100:.1f}%"
                       for k, v in sorted((geo.get("weights") or {}).items(), key=lambda kv: -kv[1]))
    source = {"sec_10k": "SEC 10-K", "trendlyne_annual_report": "annual report via Trendlyne",
              "home_default": "home country default, no breakdown found"}.get(geo.get("source"), geo.get("source"))
    period = f" FY{geo['fiscal_period_end'][:4]}" if geo.get("fiscal_period_end") else ""
    check = (geo.get("sum_check") or {}).get("status")
    lines = [f"Revenue by economy ({source}{period}{', sum check ' + check if check else ''}): {shares}"]
    s = geo.get("sector_detail") or {}
    if s.get("sector"):
        basis = f"SIC {s.get('sic')}" if s.get("source") == "sic" else "Yahoo"
        lines.append(f"Sector: {s['sector']} (from {basis}; Yahoo: {s.get('yahoo_sector')}, {s.get('yahoo_industry')})")
    warn = [f for f in geo.get("flags", []) if f != "from_cache"]
    if warn:
        lines.append(f"  Notes: {', '.join(warn)}")
    return lines


_SCORE_NAMES = {"business_quality": "Business quality", "valuation": "Valuation", "macro_fit": "Economic fit",
                "news": "News", "forecast": "Forecast"}


def _score_lines(state: GraphState) -> list[str]:
    """The 9a sub-scores (-2 to +2) and the findings behind each."""
    scores = state.get("sub_scores")
    if not scores:
        return []
    detail = state.get("analysis_detail") or {}
    findings = state.get("findings") or []
    lines = ["Scores (each from -2 to +2):"]
    prefix = {"business_quality": ("growth", "profitability", "margin_trend", "balance_sheet", "cash_generation"),
              "valuation": ("valuation_",), "macro_fit": ("macro_",), "news": ("news",), "forecast": ("forecast",)}
    for name, label in _SCORE_NAMES.items():
        value = scores.get(name)
        d = detail.get(name) or {}
        if value is None:
            missing = ", ".join(d.get("missing") or []) or "not enough data"
            lines.append(f"  {label}: missing ({missing})")
            continue
        extra = ""
        if name == "business_quality" and d.get("used_weight", 1) < 1:
            extra = f" (missing: {', '.join(d.get('missing') or [])}, weight shared out)"
        why = reliability_reason(state.get("scorecard"), name)
        if why:
            extra += f" (low reliability: {why})"
        lines.append(f"  {label}: {value:+.2f}{extra}")
        for f in findings:
            if f["component"].startswith(prefix[name]):
                score = "" if f.get("score") is None else f"({f['score']:+g}) "
                lines.append(f"    {score}{f['text']}")
    return lines


def _projection_lines(state: GraphState) -> list[str]:
    """The 9b projection and fair value, kept apart from reported figures (14.10)."""
    proj = state.get("projection") or {}
    if proj.get("status") != "ok":
        return [f"Projection: none ({', '.join(proj.get('flags', [])) or 'not run'})"] if proj else []
    currency = state.get("listing_currency") or "USD"
    a = proj["assumptions"]
    y1, y2 = proj["years"][0], proj["years"][-1]

    def case_line(field: str, label: str, large: bool) -> str:
        fmt = (lambda v: format_large(v, currency)) if large else (lambda v: format_money(v, currency))
        parts = []
        for case in ("low", "base", "high"):
            v = proj["cases"][case][y1].get(field)
            parts.append(f"{case} {fmt(v)}" if v is not None else f"{case} n/a")
        return f"  {label} FY{y1}: " + ", ".join(parts)

    g = a["growth"]
    lines = ["Projection (projected figures, not reported ones):",
             f"  Revenue growth a year: low {g['low'] * 100:.1f}%, base {g['base'] * 100:.1f}%, high {g['high'] * 100:.1f}% "
             f"(base: average of {', '.join(a.get('recent_years') or [])}; spread {a['growth_sd'] * 100:.1f} points)",
             f"  Net margin: low {a['net_margin']['low'] * 100:.1f}%, base {a['net_margin']['base'] * 100:.1f}%, "
             f"high {a['net_margin']['high'] * 100:.1f}%",
             case_line("revenue", "Revenue", True), case_line("eps", "EPS", False)]
    base2 = proj["cases"]["base"][y2]
    lines.append(f"  Base case FY{y2} (trajectory only): revenue {format_large(base2['revenue'], currency)}"
                 + (f", EPS {format_money(base2['eps'], currency)}" if base2.get("eps") is not None else ""))
    if proj.get("flags"):
        lines.append(f"  Flags: {', '.join(proj['flags'])}")

    fv = state.get("fair_value") or {}
    if fv.get("status") != "ok":
        lines.append(f"  Fair value: none, because {fv.get('reason', 'it could not be worked out')}")
        return lines
    names = {"pe": "forward P/E", "ev_ebitda": "EV/EBITDA", "pb": "P/B"}
    for m, vals in fv["methods"].items():
        lines.append(f"  {names[m]} at peer median {fv['multiples'][m]:.1f}: "
                     + ", ".join(f"{c} {format_money(v, currency)}" for c, v in vals.items()))
    r = fv["range"]
    up = fv.get("upside", {}).get("base")
    lines.append(f"  Fair value range: {format_money(r.get('low', r['base']), currency)} to "
                 f"{format_money(r.get('high', r['base']), currency)}, base {format_money(r['base'], currency)}"
                 + (f" ({up * 100:+.0f}% against today's price)" if up is not None else ""))
    if fv.get("flags"):
        lines.append(f"  Fair value notes: {'; '.join(fv['flags'])}")
    return lines


_VERDICT_WORDS = {"favourable": "Favourable", "neutral": "Neutral", "unfavourable": "Unfavourable",
                  "insufficient_data": "Insufficient data"}


def _scorecard_lines(state: GraphState) -> list[str]:
    """9c: the weighted score, any caps, the preliminary verdict and its confidence."""
    card = state.get("scorecard")
    if not card:
        return []
    if card.get("status") == "insufficient_data":
        return ["Preliminary verdict: Insufficient data, because " + "; ".join(card.get("reasons", [])),
                "  The findings above are incomplete; no verdict and no amount are given."]
    names = {"business_quality": "quality", "valuation": "valuation", "macro_fit": "economy", "news": "news",
             "forecast": "forecast"}
    used = ", ".join(f"{names.get(k, k)} {w * 100:.0f}%" for k, w in card.get("weights_used", {}).items())
    lines = [f"Scorecard: weighted score {card['score']:+.2f} (weights used: {used})"]
    if card.get("missing"):
        lines.append(f"  Missing, weight shared out: {', '.join(card['missing'])}")
    for c in card.get("caps", []):
        lines.append(f"  Capped at neutral ({c['kind']}): {c['text']}")
    for n in state.get("suitability_notes") or []:
        if not any(n == c["text"] for c in card.get("caps", [])):
            lines.append(f"  Note: {n}")
    conf = card.get("confidence") or {}
    verdict = _VERDICT_WORDS.get(card["verdict"], card["verdict"])
    raw = f" (scorecard alone: {_VERDICT_WORDS.get(card['raw_verdict'])})" if card.get("capped") else ""
    lines.append(f"Preliminary verdict: {verdict}{raw}, confidence {conf.get('level')} "
                 f"({'; '.join(conf.get('reasons', []))}; not yet calibrated)")
    return lines


def _synthesis_lines(state: GraphState) -> list[str]:
    """Step 10: the reviewed verdict, the checked reasoning and what the guardrail did."""
    if not state.get("synthesis"):
        return []
    syn = state["synthesis"]
    verdict = _VERDICT_WORDS.get(state.get("verdict"), state.get("verdict"))
    lines = ["", f"Verdict: {verdict}, confidence {state.get('confidence')}"]
    if state.get("verdict_change_reason"):
        lines.append(f"  Moved one step from the scorecard: {state['verdict_change_reason']}")
    if syn.get("templated"):
        lines.append("  (Reasoning built from the facts directly, without the model.)")
    lines.append("Reasoning:")
    lines += [f"  {p['text']}" for p in state.get("reasoning") or []]
    if state.get("key_risks"):
        lines.append("Key risks:")
        lines += [f"  - {r['text']}" for r in state["key_risks"]]
    if state.get("what_would_change_the_view"):
        lines.append("What would change the view:")
        lines += [f"  - {w}" for w in state["what_would_change_the_view"]]
    facts = {f["id"]: f for f in state.get("facts") or []}
    cited = list(dict.fromkeys(c for p in (state.get("reasoning") or []) + (state.get("key_risks") or [])
                               for c in p.get("facts", [])))
    if cited:
        lines.append("Facts cited:")
        lines += [f"  [{c}] {facts[c]['text']}" for c in cited if c in facts]
    numbers = [e for e in state.get("guardrail_log") or [] if e.get("check") == "numbers"]
    checked = sum(len(e.get("numbers", [])) for e in numbers)
    removed = state.get("removed_sentences") or []
    gflags = [f for f in state.get("guardrail_flags") or []]
    lines.append(f"Guardrail: {checked} numbers checked, {len(removed)} sentence(s) removed"
                 + (f"; {', '.join(gflags)}" if gflags else ""))
    lines += [f"  Removed: {r}" for r in removed]
    return lines


def _sizing_lines(state: GraphState) -> list[str]:
    """Step 11: the amount, the share count and how they were reached."""
    s = state.get("sizing")
    if not s:
        return []
    lines = [""]
    if s["status"] == "withheld":
        lines.append("Amount: none")
    elif s["status"] == "zero":
        lines.append(f"Amount: {format_money(0, s['currency'])} (nothing suggested)")
    elif s["shares"] == 0:
        conv = (f" (about {format_money(s['suggested_stock'], s['stock_currency'])}, indicative)"
                if s["stock_currency"] != s["currency"] else "")
        lines.append(f"Amount suggested: about {format_money(s['suggested'], s['currency'])}{conv}, less than the "
                     f"price of one share ({format_money(s['price'], s['stock_currency'])})")
    else:
        conv = (f" (about {format_money(s['amount_stock'], s['stock_currency'])}, indicative)"
                if s["stock_currency"] != s["currency"] else "")
        lines.append(f"Amount: about {format_money(s['amount'], s['currency'])}{conv}, which buys {s['shares']} "
                     f"shares at {format_money(s['price'], s['stock_currency'])}")
    lines += [f"  {n}" for n in s.get("notes", [])]
    for key in ("overexposure_note", "liquidity_note"):
        if s.get(key):
            lines.append(f"  {s[key]}")
    if s.get("steps"):
        lines.append("  How it was worked out:")
        lines += [f"    {st['step']}. {st['text']}" for st in s["steps"]]
    return lines
