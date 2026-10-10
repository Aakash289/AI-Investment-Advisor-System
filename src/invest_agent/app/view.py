"""
app/view.py: turns the agent's results into what the app shows (build step 16).

What this file does:
    Plain functions, no Gradio, so they can be tested on their own. Each takes the final
    state from main.run_turn (or a stored row) and returns HTML or text for one part of
    the screen:

        progress_html     the live list of steps while a run is going (about 100 seconds)
        card_html         the result card: verdict, confidence, amount, score bars,
                          return target, fair value and the backtest note
        question_html     the card while the agent waits for an answer (which company,
                          missing profile details)
        chat_text         the answer for the chat, without the PDF's file path (the card
                          has a download button instead)
        chart_image       the five-year share price chart, drawn by the same code as the PDF
        profile_html      what the agent has saved about you
        run_html          behind the scenes: time, model cost, step timings, source
                          health and free-plan usage
        decisions_html    the latest analyses from the decision log

    Every value shown comes from the same formatting code as the PDF (reporting/
    report_data.py), so the app and the report always say the same thing. All text is
    escaped before it goes into HTML.
"""

from __future__ import annotations

import html
import io
import re
from datetime import UTC, datetime
from typing import Any

from invest_agent.compliance.disclaimer import VERDICT_WORDS
from invest_agent.profile.budget_parser import format_amount
from invest_agent.reporting import charts, report_data

E = html.escape


# --- Progress while a run is going ------------------------------------------------------------

# The graph's steps, grouped into what a user would recognise. Each group is done when
# all its steps are; the retrieval agents run side by side, so that group lists which
# are still going.
STEPS: list[tuple[str, tuple[str, ...]]] = [
    ("Understanding your question", ("entity_resolution", "profile_intake", "input_gate")),
    ("Gathering prices, financials, the economy, news and geography",
     ("technical_price", "fundamentals", "macro", "news", "geo_sector")),
    ("Checking the figures", ("currency_normalisation", "verification")),
    ("Scoring the company", ("analysis",)),
    ("Writing the reasoning", ("synthesis",)),
    ("Checking every number in it", ("grounding",)),
    ("Working out an amount", ("sizing",)),
    ("Final checks and the PDF report", ("preview_output", "compliance", "report")),
]
RETRIEVAL_NAMES = {"technical_price": "prices", "fundamentals": "financials", "macro": "economy",
                   "news": "news", "geo_sector": "geography"}


def progress_html(steps: dict[str, dict], elapsed: float) -> str:
    """steps: {node: {"state": "running" or "done", "secs": float}} from the on_step events."""
    rows = []
    for label, nodes in STEPS:
        seen = [steps[n] for n in nodes if n in steps]
        running = [n for n in nodes if steps.get(n, {}).get("state") == "running"]
        if not seen:
            state, extra = "", ""
        elif running:
            state = "running"
            extra = (" (waiting for " + ", ".join(RETRIEVAL_NAMES[n] for n in running) + ")"
                     if all(n in RETRIEVAL_NAMES for n in running) and len(nodes) > 1 else "")
        else:
            state, extra = "done", ""
        secs = sum(s.get("secs") or 0 for s in seen if s.get("state") == "done")
        secs_text = f"{secs:.0f} s" if state == "done" and secs >= 1 else ""
        rows.append(f'<div class="isa-step {state}"><span class="dot"></span>{E(label + extra)}'
                    f'<span class="secs">{secs_text}</span></div>')
    return (f'<div class="isa-progress"><div class="head">Working on it, {elapsed:.0f} s so far '
            f'(a full analysis takes about 1 to 2 minutes)</div>{"".join(rows)}</div>')


# --- The result card --------------------------------------------------------------------------

EMPTY = ('<div class="isa-empty"><b>Ask about any company listed in India or the US.</b><br>'
         'Put your budget, risk tolerance, horizon and goal in the same message if you like, '
         'for example: <i>"Should I invest in Reliance Industries? I have 2 lakhs, fairly conservative, '
         '10 years, growth."</i><br>The verdict, the suggested amount, the price chart and the full '
         'PDF report will appear here.</div>')


def question_html(question: str) -> str:
    return (f'<div class="isa-card"><div class="isa-label">Waiting for your answer</div>'
            f'<div>{E(question)}</div><div class="isa-note">Reply in the chat; the analysis carries on '
            f'from there.</div></div>')


def _bar(value: float | None, kind: str = "") -> str:
    if value is None:
        return '<div class="isa-track"><span class="zero"></span></div>'
    v = max(-2.0, min(2.0, float(value)))
    width = abs(v) / 4 * 100                     # -2 to +2 spans the whole track
    left = 50 if v >= 0 else 50 - width
    cls = kind or ("pos" if v >= 0 else "neg")
    return (f'<div class="isa-track"><span class="fill {cls}" style="left:{left:.1f}%;width:{width:.1f}%"></span>'
            f'<span class="zero"></span></div>')


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{E(i)}</li>" for i in items) + "</ul>" if items else ""


def card_html(result: dict) -> str:
    """The result card for a finished analysis (or a plain message when there was none)."""
    verdict = result.get("verdict")
    if not verdict or verdict == "not_covered" or not result.get("disclaimer_version"):
        return (f'<div class="isa-card"><div class="isa-label">No analysis this time</div>'
                f'<div>{E(chat_text(result))}</div></div>')
    try:
        data = report_data.build(result)
    except Exception as err:                    # noqa: BLE001 - a card problem must not hide the chat answer
        return (f'<div class="isa-card"><div class="isa-label">Result</div><div>{E(VERDICT_WORDS.get(verdict, verdict))}'
                f'</div><div class="isa-note">The summary card could not be drawn ({E(type(err).__name__)}); '
                f'the answer is in the chat.</div></div>')

    cls = f"isa-{verdict}" if verdict in ("favourable", "neutral", "unfavourable") else "isa-other"
    parts = [f'<div class="isa-card"><h2>{E(data["title"])}</h2>',
             f'<div class="isa-sub">{E(data["date"])} · {E(data["profile"])}</div>',
             (f'<div class="isa-badges"><span class="isa-badge {cls}">{E(data["verdict"])}</span>'
              f'<span class="isa-badge isa-conf">Confidence: {E(str(data["confidence"]).lower())}</span></div>')]

    if data.get("near_line"):
        parts.append(f'<div class="isa-note" style="margin-top:0">{E(data["near_line"])}</div>')
    parts.append(f'<div class="isa-label">Suggested amount</div><div class="isa-amount">{E(data["amount"])}</div>')
    parts.append(_list(data.get("amount_notes") or []))

    # Score bars: each sub-score, then the weighted total.
    card = result.get("scorecard") or {}
    subs = result.get("sub_scores") or {}
    titles = {k: row[0] for k, row in zip(report_data.SUB_ORDER, data["scorecard"])}
    rows = []
    for k in report_data.SUB_ORDER:
        value = subs.get(k)
        val = f"{float(value):+.2f}" if value is not None else '<span class="isa-missing">missing</span>'
        rows.append(f'<div class="name">{E(titles.get(k, k))}</div>{_bar(value)}<div class="val">{val}</div>')
    if card.get("score") is not None:
        rows.append(f'<div class="name"><b>Weighted score</b></div>{_bar(card["score"], "total")}'
                    f'<div class="val"><b>{card["score"]:+.2f}</b></div>')
    parts.append(f'<div class="isa-label">Scorecard (-2 to +2; {E(report_data._lines_text())})</div>'
                 f'<div class="isa-bars">{"".join(rows)}</div>')

    if data.get("target"):
        parts.append('<div class="isa-label">Your return target</div>' + _list(data["target"]))
    glance = dict(data.get("glance") or [])
    if glance.get("Fair value a year out"):
        parts.append(f'<div class="isa-label">Fair value a year out</div><div>{E(glance["Fair value a year out"])}</div>')
    if data.get("currency_note"):
        parts.append(f'<div class="isa-label">Currency</div><div>{E(data["currency_note"])}</div>')
    if data.get("key_risks"):
        parts.append('<div class="isa-label">Key risks</div>' + _list([_untag(r) for r in data["key_risks"][:3]]))
    if data.get("backtest_note"):
        parts.append(f'<div class="isa-note">{E(data["backtest_note"])}</div>')
    parts.append("</div>")
    return "".join(parts)


def _untag(text: str) -> str:
    """Drop the numbered source marks ([1], [2, 3]) the report uses; the card has no source list."""
    return re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]", "", text)


# --- The chat answer and the chart -------------------------------------------------------------

def chat_text(result: dict) -> str:
    """The compact answer for the chat. The PDF path line is replaced, since the card has a
    download button (and a file path means nothing in a browser)."""
    text = result.get("final_response") or ""
    if result.get("report_path"):
        text = re.sub(r"Full report \(PDF\): .+", "Full report: download the PDF from the panel on the right.", text)
    return text


def chart_image(result: dict) -> Any | None:
    """The five-year share price chart as a PIL image, or None when there is no price history."""
    chart = result.get("price_chart") or {}
    if not chart.get("stock"):
        return None
    symbol = "₹" if (result.get("listing_currency") or "") == "INR" else "$"
    names = {"^NSEI": "Nifty 50", "^GSPC": "S&P 500"}
    png = charts.price_chart(chart["dates"], chart["stock"], chart.get("benchmark"), symbol,
                             names.get(chart.get("benchmark_symbol"), chart.get("benchmark_symbol")))
    if not png:
        return None
    # Pillow (PIL) comes with matplotlib; Gradio's image box shows a PIL image directly.
    from PIL import Image
    return Image.open(io.BytesIO(png))


# --- Side tabs ---------------------------------------------------------------------------------

def local_time(iso: str | None) -> str:
    """A stored UTC time shown in this computer's own time zone (India Standard Time for
    Aakash), as "07 Oct 2026, 18:04". The database keeps UTC; only the display changes."""
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)[:16].replace("T", " ")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().strftime("%d %b %Y, %H:%M")


def _table(head: list[str], rows: list[list[Any]]) -> str:
    th = "".join(f"<th>{E(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{E(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="isa-table"><tr>{th}</tr>{body}</table>'


PROFILE_LABELS = [("budget", "Budget"), ("risk_tolerance", "Risk tolerance"), ("horizon_years", "Horizon"),
                  ("goal", "Goal"), ("concentration_limit_pct", "Most in one stock"),
                  ("liquidity_needs", "May need the money at short notice")]


def profile_html(profile: dict, updated_at: str | None) -> str:
    if not profile:
        return ('<div class="isa-empty">Nothing saved yet. Mention your budget, risk tolerance, horizon and goal '
                'in a question and they will be remembered for next time.</div>')
    shown = {
        "budget": format_amount(profile["budget_amount"], profile.get("budget_currency"))
        if profile.get("budget_amount") else None,
        "risk_tolerance": profile.get("risk_tolerance"),
        "horizon_years": f"{profile['horizon_years']:g} years" if profile.get("horizon_years") else None,
        "goal": str(profile.get("goal") or "").replace("_", " ") or None,
        "concentration_limit_pct": f"{profile['concentration_limit_pct']:g}% of the budget"
        if profile.get("concentration_limit_pct") else None,
        "liquidity_needs": None if profile.get("liquidity_needs") is None else ("yes" if profile["liquidity_needs"] else "no"),
    }
    rows = [[label, shown[k] if shown[k] is not None else "not given"] for k, label in PROFILE_LABELS]
    when = f"Last saved {local_time(updated_at)}. " if updated_at else ""
    return (_table(["", "Saved"], rows)
            + f'<div class="isa-note">{E(when)}To change something, say it in your next question '
              f'("my budget is now 5 lakhs, should I buy Infosys?"). A return target is used for one question '
              f'only and is never saved.</div>')


def run_html(result: dict | None, health: dict[str, str], quota_rows: list[dict]) -> str:
    parts = []
    run = (result or {}).get("run")
    if run:
        cost = run.get("cost") or {}
        parts.append(f'<div class="isa-label">Last run</div><div>{run.get("latency_s")} s, '
                     f'{len(run.get("calls") or [])} outside calls, model cost ${cost.get("total_usd", 0):.4f}</div>')
        timings = sorted(run.get("timings") or [], key=lambda t: t["start_s"])
        if timings:
            parts.append('<div class="isa-label">Step timings</div>'
                         + _table(["Step", "Started at", "Took"],
                                  [[t["node"], f"{t['start_s']:.1f} s", f"{t['duration_s']:.1f} s"] for t in timings]))
        for w in run.get("quota_warnings") or []:
            parts.append(f'<div class="isa-note">Usage limit: {E(w)}</div>')
    else:
        parts.append('<div class="isa-note">Run an analysis to see how long each step took and what it cost.</div>')
    parts.append('<div class="isa-label">Source health</div>'
                 + _table(["Source", "Status"], [[k, v] for k, v in sorted(health.items())]))
    parts.append('<div class="isa-label">Free-plan usage</div>'
                 + _table(["Service", "Period", "Used", "Limit"],
                          [[q["service"], q["period"], f"{q['used']:,}", f"{q['limit']:,}"] for q in quota_rows]))
    return "".join(parts)


def decisions_html(rows: list[dict]) -> str:
    if not rows:
        return '<div class="isa-note">No analyses logged yet.</div>'
    out = []
    for r in rows:
        amount = (f"{r['recommended_amount']:,.0f} {r.get('amount_currency') or ''}".strip()
                  if r.get("recommended_amount") is not None else "")
        out.append([local_time(r.get("created_at")), r.get("ticker") or "",
                    VERDICT_WORDS.get(r.get("verdict"), r.get("verdict") or ""), r.get("confidence") or "", amount,
                    f"{r['latency_s']:.0f} s" if r.get("latency_s") is not None else ""])
    return _table(["When", "Company", "Verdict", "Confidence", "Amount", "Time"], out)
