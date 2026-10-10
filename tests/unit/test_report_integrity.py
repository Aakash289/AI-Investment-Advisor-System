"""
tests/unit/test_report_integrity.py: the PDF report and its integrity checks (design 14.14).

What this file checks:
    the formatter       negative amounts, missing values ("Not available")
    sources             [F3, F7] become numbered references in order of first citation;
                        a worked-out figure is listed once, in the fact's own words
    integrity checks    no disclaimer, no PDF; a reference with no source; a blank cell
    projection          left out, with the reason, when no figure carries the tag
    the files           a real PDF and its JSON data object are saved side by side
    the node            the report line goes just above the disclaimer, which stays last;
                        a failure leaves the answer and says so
"""

from __future__ import annotations

import json

import pytest

from invest_agent.common.records import make_record
from invest_agent.reporting import render, report_data
from invest_agent.reporting.render import ReportError, check
from invest_agent.reporting.report_data import NA, Sources, show
from invest_agent.reporting.step import _insert_line, report_node


def rec(field, value, year=None, source="trendlyne"):
    extra = {"fiscal_year": year, "period": f"FY{year}"} if year else {}
    return make_record(source=source, tier=2, entity="X", field=field, value=value, as_of="2026-03-31", **extra)


def state():
    rows = [rec("revenue", 1e12 + y * 1e10, y) for y in range(2021, 2027)]
    rows += [rec("net_income", 7e10, y) for y in range(2021, 2027)] + [rec("current_price", 1216.0, source="yfinance")]
    facts = [{"id": "F1", "block": "business", "text": "FY2026: revenue ₹1,02,000 crore",
              "record_ids": [rows[5]["record_id"]], "kind": "reported"},
             {"id": "F2", "block": "valuation", "text": "P/E 21.5 against a peer median of 9.6",
              "record_ids": ["computed:X:peer_median_pe"], "kind": "computed"}]
    return {"company_name": "X Ltd", "ticker": "X", "exchange": "NSE", "listing_currency": "INR",
            "verdict": "neutral", "confidence": "low", "disclaimer_version": "1",
            "user_profile": {"budget_amount": 200000, "budget_currency": "INR", "risk_tolerance": "low",
                             "horizon_years": 10, "goal": "growth"},
            "verified_records": rows, "facts": facts,
            "reasoning": [{"text": "Revenue was ₹1,02,000 crore [F1]. The P/E is 21.5 [F2]."}],
            "key_risks": [{"text": "The P/E is 21.5 [F2]."}], "what_would_change_the_view": [],
            "sizing": {"status": "zero", "notes": ["No amount."], "steps": []},
            "final_response": "Information only.\n\nBody\n\nDisclaimer: full text"}


# --- Formatter and sources ----------------------------------------------------------------------

def test_show():
    assert show(-1e11, "large", "INR") == "-₹10,000 crore"
    assert show(None) == NA and show(0.123, "pct") == "12.3%"


def test_sources_are_numbered_in_order_of_citation():
    s = state()
    src = Sources(s["facts"], report_data.all_records(s))
    assert src.retag("A [F2]. B [F1, F2].") == "A [1]. B [1, 2]."
    listing = src.listing()
    assert listing[0]["source"] == "worked out in this analysis" and "P/E 21.5" in listing[0]["field"]
    assert listing[1]["source"] == "trendlyne"


# --- Integrity checks ---------------------------------------------------------------------------

def test_no_disclaimer_no_pdf(tmp_path):
    data = report_data.build(state())
    data["disclaimer"]["full"] = ""
    with pytest.raises(ReportError, match="no disclaimer"):
        render.save(data, str(tmp_path), "X")
    assert not list(tmp_path.iterdir())


def test_reference_without_a_source():
    data = report_data.build(state())
    data["reasoning"].append("Something [9].")
    with pytest.raises(ReportError, match=r"\[9\]"):
        check(data)


def test_blank_cell():
    data = report_data.build(state())
    data["economy"] = [["India", "", "rates steady"]]
    with pytest.raises(ReportError, match="blank"):
        check(data)


def test_projection_needs_its_tag():
    s = {**state(), "projection": {"status": "ok", "flags": []}, "projection_records": []}
    data = report_data.build(s)
    assert data["projection"] is None and data["no_projection"].startswith("No projection")


# --- Files and node -----------------------------------------------------------------------------

def test_pdf_and_data_object_are_saved(tmp_path):
    path = render.save(report_data.build(state()), str(tmp_path), "X")
    assert open(path, "rb").read(5) == b"%PDF-"
    data = json.load(open(path.replace(".pdf", ".json"), encoding="utf-8"))
    assert data["reasoning"][0] == "Revenue was ₹1,02,000 crore [1]. The P/E is 21.5 [2]."


def test_report_line_goes_above_the_disclaimer():
    text = _insert_line("Info.\n\nBody\n\nDisclaimer: full", "Full report (PDF): x.pdf")
    assert text.endswith("Disclaimer: full") and "Full report (PDF): x.pdf\n\nDisclaimer:" in text


def test_node_saves_and_failure_is_reported(monkeypatch):
    out = report_node(state())
    assert out["report_path"].endswith(".pdf") and "Full report (PDF):" in out["final_response"]

    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(render, "save", broken)
    out = report_node(state())
    assert "could not be produced" in out["final_response"] and "disk full" in out["report_error"]
    assert out["final_response"].endswith("Disclaimer: full text")


def test_no_report_when_not_covered():
    assert report_node({"verdict": "not_covered", "disclaimer_version": "1"}) == {}


def test_scorecard_explained_and_glossary():
    s = {**state(), "sub_scores": {"business_quality": 0.5, "valuation": -2.0, "macro_fit": 0.18, "news": 0.44,
                                   "forecast": None},
         "scorecard": {"score": -0.12, "raw_verdict": "neutral", "low_reliability": ["valuation"],
                       "weights_used": {"business_quality": 0.444, "valuation": 0.222, "macro_fit": 0.167,
                                        "news": 0.167}}}
    data = report_data.build(s)
    subs = {x["key"]: x for x in data["subscores"]}
    assert set(subs) == {"business_quality", "valuation", "macro_fit", "news", "forecast"}
    assert all(x["what"] and x["how"] and x["use"] for x in subs.values())
    assert subs["valuation"]["low_reliability"] and subs["forecast"]["score"] == "Missing"
    assert data["scorecard"][1] == ["Valuation (low reliability)", "-2.00", "22%", "-0.44"]
    terms = {row[0]: row for row in data["glossary"]}
    assert terms["P/E"][1] == "Price to Earnings" and terms["ROIC"][1] == "Return On Invested Capital"
    assert data["glance"][0] == ["Verdict", "Neutral"]
