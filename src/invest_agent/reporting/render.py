"""
reporting/render.py: draws the PDF report and saves it (design 14.14).

What this file does:
    1. Checks the report data object before drawing anything (integrity checks):
         the disclaimer is there; without it there is no PDF at all (the one hard
           failure)
         every numbered reference in the text points to an entry in the Sources list
         no table cell is blank (a missing value says "Not available")
         the projection section is drawn only when its figures carry the projection tag
    2. Draws the report with ReportLab, in the clean broker-note style: white page, one
       accent colour (deep blue), serif headings, a sans-serif body, tables with light
       rules, the projection in a shaded box. A cover page (the disclaimer box, the
       answer at a glance, the contents), then thirteen numbered sections, each opening
       with one line on what it shows (reworked 6 October at Aakash's request):
         1 summary   2 the business   3 share price (five years against the market
         index)   4 valuation   5 projection   6 economic context   7 news and disclosures
         8 reasoning   9 the scorecard explained (each sub-score: what it measures, how it
         is worked out, this company's parts, how to use it)   10 how the amount was
         worked out   11 data notes and limits   12 sources   13 glossary (every term and
         short form, with its full form and meaning)
       The fixed wording lives in report_text.yaml.
    3. Saves the PDF and the data object (as JSON) side by side in data/reports, named
       by ticker and time, so what the person was shown can be checked or drawn again.

    Why ReportLab and not WeasyPrint (decided 6 October): WeasyPrint needs the Pango
    text library, which on Windows means installing GTK separately; ReportLab installs
    with pip alone. The design named ReportLab as the alternative for exactly this case.
    The fonts are the DejaVu fonts that come with matplotlib, which include the rupee
    sign.

    A failure here never blocks the answer: the node records the error and the answer
    says the report could not be produced.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from invest_agent.reporting import charts
from invest_agent.reporting.report_data import NA

logger = logging.getLogger(__name__)

ACCENT = colors.HexColor("#1f3a5f")
RULE = colors.HexColor("#c9d3de")
SHADE = colors.HexColor("#eef2f6")
REF = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


class ReportError(Exception):
    pass


# --- Fonts and styles ---------------------------------------------------------------------------

def _fonts() -> None:
    if "Body" in pdfmetrics.getRegisteredFontNames():
        return
    d = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    for name, file in (("Body", "DejaVuSans.ttf"), ("Body-Bold", "DejaVuSans-Bold.ttf"),
                       ("Head", "DejaVuSerif.ttf"), ("Head-Bold", "DejaVuSerif-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(d / file)))


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("body", fontName="Body", fontSize=9, leading=12.5, alignment=TA_LEFT)
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.5, leading=10),
        "title": ParagraphStyle("title", parent=base, fontName="Head-Bold", fontSize=18, leading=22,
                                textColor=ACCENT, spaceAfter=4),
        "h1": ParagraphStyle("h1", parent=base, fontName="Head-Bold", fontSize=12.5, leading=16,
                             textColor=ACCENT, spaceBefore=10, spaceAfter=4),
        "box": ParagraphStyle("box", parent=base, fontSize=8, leading=11),
        "big": ParagraphStyle("big", parent=base, fontName="Body-Bold", fontSize=11, leading=15),
        "h2": ParagraphStyle("h2", parent=base, fontName="Head-Bold", fontSize=10.5, leading=14,
                             textColor=ACCENT, spaceBefore=8, spaceAfter=3),
        "intro": ParagraphStyle("intro", parent=base, fontSize=8.5, leading=11.5, textColor=colors.HexColor("#55606e"),
                                spaceAfter=5),
        "label": ParagraphStyle("label", parent=base, fontName="Body-Bold", fontSize=7.5, leading=10, spaceBefore=2),
    }


def p(text: str | None, style) -> Paragraph:
    """A paragraph of plain text (escaped). None prints "Not available"; an empty string
    (a table's corner cell) prints nothing."""
    return Paragraph(escape(NA if text is None else text), style)


def _table(rows: list[list[str]], widths=None, header: bool = True, rules: bool = True,
           bold_first: bool = False) -> Table:
    st = _styles()
    bold = ParagraphStyle("cellbold", parent=st["small"], fontName="Body-Bold")
    head = ParagraphStyle("cellhead", parent=st["small"], fontName="Body-Bold", textColor=ACCENT)
    data = [[p(c, head if header and r_i == 0 else bold if bold_first and c_i == 0 else st["small"])
             for c_i, c in enumerate(r)] for r_i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, hAlign="LEFT")
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if rules:
        style += [("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE)]
    if header:
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT)]
    t.setStyle(TableStyle(style))
    return t


def _box(flowables: list, shade=SHADE, border=ACCENT) -> Table:
    t = Table([[flowables]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), shade), ("BOX", (0, 0), (-1, -1), 0.8, border),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def _image(png: bytes | None, width_mm: float):
    if not png:
        return None
    import io
    img = Image(io.BytesIO(png))
    ratio = img.imageHeight / img.imageWidth
    img.drawWidth, img.drawHeight = width_mm * mm, width_mm * mm * ratio
    return img


# --- Integrity checks ---------------------------------------------------------------------------

def check(data: dict) -> None:
    d = data.get("disclaimer") or {}
    if not (d.get("short") or "").strip() or not (d.get("full") or "").strip():
        raise ReportError("no disclaimer, so no PDF")
    n_sources = len(data.get("sources") or [])
    for text in data.get("reasoning", []) + data.get("key_risks", []) + data.get("what_would_change", []):
        for group in REF.findall(text):
            for n in group.split(","):
                if not 1 <= int(n) <= n_sources:
                    raise ReportError(f"reference [{n.strip()}] has no entry in the sources list")
    tables = [data.get("history", {}).get("table", []), data.get("valuation", {}).get("rows", []),
              data.get("economy", []), data.get("scorecard", []), data.get("glance", [])]
    if data.get("projection"):
        tables.append(data["projection"]["table"])
    for t in tables:
        for row in t:
            if any(c is None or str(c).strip() == "" for c in row):
                raise ReportError("a table cell is blank")


# --- Drawing ------------------------------------------------------------------------------------

SECTIONS = [("summary", "Summary"), ("business", "The business"), ("price", "Share price"),
            ("valuation", "Valuation against similar companies"),
            ("projection", "Projection"), ("economy", "Economic context"), ("news", "Recent news and disclosures"),
            ("reasoning", "Reasoning"), ("scorecard", "The scorecard explained"),
            ("amount", "How the amount was worked out"), ("notes", "Data notes and limits"), ("sources", "Sources"),
            ("glossary", "Glossary")]


def draw(data: dict, path: str) -> None:
    check(data)
    _fonts()
    st = _styles()
    story: list = []
    add = story.append
    number = {key: i + 1 for i, (key, _) in enumerate(SECTIONS)}
    intros = data.get("section_intros") or {}

    def heading(key: str) -> None:
        title = dict(SECTIONS)[key]
        add(p(f"{number[key]}. {title}", st["h1"]))
        if intros.get(key):
            add(p(intros[key], st["intro"]))

    # Cover: the disclaimer box first, then the company, the answer at a glance, contents.
    add(_box([p(data["disclaimer"]["short"], st["big"]), Spacer(1, 3), p(data["disclaimer"]["full"], st["box"])],
             shade=colors.HexColor("#fbf6ec"), border=colors.HexColor("#b08a3a")))
    add(Spacer(1, 10))
    add(p(data["title"], st["title"]))
    add(p(f"Investment suitability analysis, {data['date']}", st["body"]))
    add(p(f"Prepared for: {data['profile']}", st["body"]))
    add(Spacer(1, 8))
    add(p("At a glance", st["h2"]))
    add(_table(data["glance"], widths=[45 * mm, 125 * mm], header=False, bold_first=True))
    add(Spacer(1, 8))
    add(p("Contents", st["h2"]))
    add(_table([[f"{number[k]}.", t] for k, t in SECTIONS], widths=[10 * mm, 160 * mm], header=False, rules=False))
    add(PageBreak())

    # 1 Summary.
    heading("summary")
    summary = [p(f"Verdict: {data['verdict']}, confidence {data['confidence']}", st["big"]),
               p(f"Suggested amount: {data['amount']}", st["body"])]
    summary += [p(n, st["small"]) for n in data["amount_notes"]]
    if data.get("verdict_change"):
        summary.append(p(f"Moved one step from the scorecard: {data['verdict_change']}", st["small"]))
    elif data.get("near_line"):
        summary.append(p(data["near_line"], st["small"]))
    if data.get("backtest_note"):
        summary.append(p(data["backtest_note"], st["small"]))
    add(_box(summary))
    if data.get("currency_note"):
        add(Spacer(1, 4))
        add(p(f"Currency: {data['currency_note']}", st["small"]))
    for s_line in data.get("situations", []):
        add(p(s_line, st["small"]))
    if data.get("target"):
        add(p("Your return target", st["h2"]))
        story.extend(p(t, st["body"]) for t in data["target"])
    if data["key_risks"]:
        add(p("Main risks", st["h2"]))
        story.extend(p(f"\u2022 {r}", st["body"]) for r in data["key_risks"])
    if data["what_would_change"]:
        add(p("What would change the view", st["h2"]))
        story.extend(p(f"\u2022 {w}", st["body"]) for w in data["what_would_change"])

    # 2 The business.
    heading("business")
    h = data["history"]
    if h["years"]:
        add(_table([[""] + h["years"]] + h["table"], widths=[32 * mm] + [23 * mm] * len(h["years"])))
        sym = "\u20b9" if "\u20b9" in "".join(h["table"][0]) else "$"
        scale, unit = (1e7, "\u20b9 crore") if sym == "\u20b9" else (1e9, "$ billion")
        imgs = [_image(charts.revenue_chart(h["years"], h["series"]["revenue"], unit, scale), 84),
                _image(charts.margin_chart(h["years"], h["series"]), 84)]
        imgs = [i for i in imgs if i]
        if imgs:
            add(Spacer(1, 6))
            add(Table([imgs], hAlign="LEFT"))
    else:
        add(p("No annual figures were available.", st["body"]))

    # 3 Share price: five years against the market index.
    heading("price")
    pr_ = data.get("price") or {}
    if pr_.get("chart"):
        c = pr_["chart"]
        sym = "\u20b9" if "\u20b9" in "".join(r[1] for r in pr_["rows"]) else "$"
        img = _image(charts.price_chart(c["dates"], c["stock"], c.get("benchmark"), sym, pr_.get("benchmark_name")), 165)
        if img:
            add(img)
            add(Spacer(1, 4))
    add(_table(pr_.get("rows") or [["Share price", NA]], widths=[60 * mm, 60 * mm], header=False, bold_first=True))
    if pr_.get("note"):
        add(p(pr_["note"], st["small"]))

    # 4 Valuation.
    heading("valuation")
    v = data["valuation"]
    add(_table([["Measure", "Company", "Peer median", "Difference"]] + v["rows"],
               widths=[30 * mm, 30 * mm, 30 * mm, 40 * mm]))
    add(Spacer(1, 3))
    add(p(f"Peers compared: {v['peers']}" + (f". Left out: {v['left_out']}" if v["left_out"] else ""), st["small"]))
    if v["weak"]:
        add(Spacer(1, 3))
        add(_box([p("Low reliability: the company is far dearer than every peer, which usually means the peers "
                    "cover only part of its business (for example a conglomerate compared with one of its "
                    "industries). Read this comparison with caution.", st["small"])],
                 shade=colors.HexColor("#fbf6ec"), border=colors.HexColor("#b08a3a")))
    if v.get("fallback_note"):
        add(Spacer(1, 3))
        add(_box([p(v["fallback_note"], st["small"])],
                 shade=colors.HexColor("#fbf6ec"), border=colors.HexColor("#b08a3a")))

    # 5 Projection, in a shaded box.
    heading("projection")
    if data.get("projection"):
        pr = data["projection"]
        inner = [p("Projection, not reported figures", st["big"])]
        inner += [p(a, st["small"]) for a in pr["assumptions"]]
        inner.append(Spacer(1, 4))
        inner.append(_table(pr["table"], widths=[45 * mm, 38 * mm, 38 * mm, 38 * mm]))
        fvd = pr.get("fair_value")
        inner.append(Spacer(1, 4))
        if fvd:
            inner.append(p(f"Estimated fair value a year out: {fvd['text']}. Based on next year's projected earnings "
                           f"at the multiples similar companies trade at today. Actual results will differ.",
                           st["body"]))
            sym = "\u20b9" if "\u20b9" in fvd["text"] else "$"
            img = _image(charts.fair_value_chart(fvd["values"], sym), 160)
            if img:
                inner.append(img)
        else:
            inner.append(p(pr.get("no_fair_value", NA), st["body"]))
        add(_box(inner))
    else:
        add(p(data.get("no_projection", NA), st["body"]))

    # 5 Economic context.
    heading("economy")
    if data["economy"]:
        add(_table([["Economy", "Share of revenue", "Conditions"]] + data["economy"],
                   widths=[35 * mm, 30 * mm, 105 * mm]))
    else:
        add(p("No revenue split was available.", st["body"]))

    # 6 News and disclosures: headlines and links only.
    heading("news")
    if data["news"]:
        for n in data["news"]:
            link = f' <link href="{escape(n["url"])}" color="#1f3a5f">(link)</link>' if n.get("url") else ""
            story.append(Paragraph(f"{escape(str(n['date']))} {escape(str(n['outlet']))}: {escape(str(n['title']))} "
                                   f"<i>({escape(n['label'])})</i>{link}", st["small"]))
    story.extend(p(f, st["small"]) for f in data["filings"])
    if not data["news"] and not data["filings"]:
        add(p("No relevant news in the last month.", st["body"]))

    # 7 Reasoning.
    heading("reasoning")
    for par in data["reasoning"] or ["No reasoning was produced."]:
        add(p(par, st["body"]))
        add(Spacer(1, 4))

    # 8 The scorecard explained.
    add(PageBreak())
    heading("scorecard")
    add(_box([p("How to read the sub-scores", st["big"]), p(data["reading"], st["small"])]))
    add(Spacer(1, 6))
    add(_table([["Sub-score", "Score (-2 to +2)", "Weight used", "Adds to the total"]] + data["scorecard"],
               widths=[60 * mm, 35 * mm, 30 * mm, 40 * mm]))
    add(p(data["scorecard_total"], st["small"]))
    if data.get("confidence_reasons"):
        add(p("Confidence: " + "; ".join(data["confidence_reasons"]) + ". Not yet calibrated against past results.",
              st["small"]))
    for sub in data["subscores"]:
        block = [p(f"{sub['title']}: {sub['score']}" + (" (low reliability)" if sub["low_reliability"] else "")
                   + f", weight {sub['weight']}", st["h2"]),
                 _label_para("What it measures", sub["what"], st),
                 _label_para("How it is worked out", sub["how"], st),
                 p("For this company:", st["label"])]
        block += [p(f"\u2022 {line}", st["small"]) for line in sub["this_company"] or ["No details."]]
        block.append(_label_para("How to use it", sub["use"], st))
        add(KeepTogether(block))

    # 9 How the amount was worked out.
    heading("amount")
    if data["sizing_steps"]:
        story.extend(p(f"{i + 1}. {s_}", st["body"]) for i, s_ in enumerate(data["sizing_steps"]))
    else:
        add(p("No amount was worked out: " + data["amount"], st["body"]))

    # 10 Data notes and limits.
    heading("notes")
    story.extend(p(n, st["small"]) for n in data["data_notes"])
    add(Spacer(1, 4))
    add(p("What this analysis cannot see", st["h2"]))
    add(p("Your other holdings and full finances; brokerage, taxes and the costs of investing abroad; anything not "
          "in public data or released after it was gathered. The confidence levels are not yet calibrated against "
          "past results.", st["small"]))
    if data.get("backtest_note"):
        add(p(data["backtest_note"], st["small"]))
    add(p(f"Scorecard settings version {data.get('config_version')}; disclaimer version "
          f"{data['disclaimer']['version']}.", st["small"]))

    # 11 Sources.
    add(PageBreak())
    heading("sources")
    for s_ in data["sources"]:
        ref = s_.get("ref") or ""
        ref_html = (f'<link href="{escape(ref)}" color="#1f3a5f">{escape(ref)}</link>' if ref.startswith("http")
                    else escape(ref))
        extra = f", filing {escape(s_['accession'])}" if s_.get("accession") else ""
        tag = " (projection)" if s_.get("projected") else ""
        when = f", {escape(str(s_['as_of']))}" if s_.get("as_of") else ""
        story.append(Paragraph(f"[{s_['n']}] {escape(s_.get('field') or '')} ({escape(s_['source'])}{tag}{when}{extra})"
                               + (f". {ref_html}" if ref_html else ""), st["small"]))

    # 12 Glossary.
    heading("glossary")
    rows = [[t, full, meaning] for t, full, meaning in data.get("glossary", [])]
    add(_table([["Term", "Full form", "Meaning"]] + [[t, f or "-", m] for t, f, m in rows],
               widths=[30 * mm, 45 * mm, 95 * mm]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Body", 7)
        canvas.setFillColor(colors.grey)
        line = f"{data['title']}, {data['date']}. {data['disclaimer']['short']}"
        if canvas.stringWidth(line, "Body", 7) > 155 * mm:
            # A long company name ran into the page number (Star Health); use the ticker.
            short = re.search(r"\(([^)]*)\)\s*$", data["title"])
            line = f"{short.group(1) if short else data['title'][:40]}, {data['date']}. {data['disclaimer']['short']}"
        canvas.drawString(20 * mm, 10 * mm, line)
        canvas.drawRightString(190 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=data["title"], author="Investment Suitability Agent")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def _label_para(label: str, text: str, st: dict) -> Paragraph:
    return Paragraph(f"<b>{escape(label)}:</b> {escape(text)}", st["small"])


def save(data: dict, reports_dir: str, ticker: str, now: datetime | None = None) -> str:
    """Draw and save the PDF and its data object. Returns the PDF path."""
    now = now or datetime.now()
    os.makedirs(reports_dir, exist_ok=True)
    stem = Path(reports_dir) / f"{re.sub(r'[^A-Za-z0-9]+', '_', ticker)}_{now:%Y%m%d_%H%M%S}"
    pdf = f"{stem}.pdf"
    draw(data, pdf)
    with open(f"{stem}.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=str)
    return pdf
