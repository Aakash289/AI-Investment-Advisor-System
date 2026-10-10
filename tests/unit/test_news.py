"""
Unit tests for build step 8b, the News agent: the Tavily, Trendlyne, SEC and GDELT
readers on answers in the formats the news probes saw on 5 October 2026, deduplication,
the spike rule, net sentiment, the server with fake sources, and the node with a fake
labelling model.

No network, no model and no credits used. Run with:
    uv run pytest tests/unit/test_news.py -v
"""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from invest_agent.agents.news import ItemLabel, Labels, apply_labels, make_news_node
from invest_agent.cache.store import CacheStore
from invest_agent.mcp_servers.news import aggregate, gdelt, sec_8k, server, spike, tavily, trendlyne_disclosures
from invest_agent.storage.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trendlyne"
TODAY = date(2026, 10, 5)


# --- Readers ---------------------------------------------------------------------------------------

def test_tavily_results_drop_price_pages_and_read_dates():
    results = [
        {"title": "RIL Share Price Today, RIL Stock Price Live NSE/BSE Updates | The Economic Times",
         "url": "https://economictimes.indiatimes.com/reliance-industries-ltd/stocks/companyid-13215.cms",
         "published_date": "Wed, 30 Sep 2026 09:00:00 GMT", "content": "x"},
        {"title": "Ambani's Jio Platforms plans to launch IPO on October 21, ...",
         "url": "https://www.reuters.com/world/india/ambanis-jio-platforms-plans-launch-ipo",
         "published_date": "Mon, 05 Oct 2026 07:00:00 GMT", "content": "Jio Platforms plans its IPO on October 21"},
    ]
    items = tavily.to_items(results, "company", trusted=True)
    assert len(items) == 1 and items[0]["date"] == "2026-10-05" and items[0]["outlet"] == "reuters.com"


def test_trendlyne_news_table():
    items = trendlyne_disclosures.parse_news((FIXTURES / "news_reliance.txt").read_text(encoding="utf-8"))
    first = items[0]
    assert first["title"].startswith("Mukesh Ambani's Jio Platforms plans") and first["title"].endswith("IPO on October 21")
    assert first["date"] == "2026-10-05" and first["outlet"] == "Business Line"
    assert first["url"].startswith("https://www.thehindubusinessline.com/")


def test_trendlyne_insider_trades_from_the_last_month():
    text = (FIXTURES / "sast_reliance.txt").read_text(encoding="utf-8")
    items = trendlyne_disclosures.parse_insider(text, date(2026, 6, 1))
    assert items[0]["title"] == "Anish Shah (Designated Person): acquisition of 880 shares, Inheritance"
    assert all(i["date"] >= "2026-05-01" for i in items) and items[0]["promoter"] is False
    # Months later, the same trades are out of the window (Star Health's July trades in October).
    assert trendlyne_disclosures.parse_insider(text, TODAY) == []


def test_promoter_transfer_becomes_one_neutral_item():
    """Star Health, 28 July: the estate's disposal and the family entity's acquisition."""
    row = '["{n}","Promoter","{a}","2026-09-28",{q},0,0,0,0,"2026-09-26","2026-09-26","SAST",null,"Equity Shares","Off Market","2026-09-28",0]'
    text = "tableData:\n  " + ", ".join([row.format(n="Estate of A", a="Disposal", q=76825899),
                                          row.format(n="Family LLP", a="Acquisition", q=78213958)])
    items = trendlyne_disclosures.parse_insider(text, date(2026, 10, 6))
    assert len(items) == 1 and items[0]["internal_transfer"]
    assert items[0]["title"].startswith("Promoter shares moved within the promoter group: Estate of A to Family LLP")
    labels = Labels(items=[ItemLabel(id="t1", relevant=True, kind="fact_report", event_type="other",
                                     sentiment="negative", materiality="high", reason="promoter sold")])
    labelled = apply_labels([{**items[0], "id": "t1"}], labels)[0]
    assert labelled["sentiment"] == "neutral" and labelled["materiality"] == "low"


def test_sec_8k_items_named_and_serious_flagged():
    payload = {"filings": {"recent": {
        "form": ["8-K", "10-Q", "8-K", "8-K"], "filingDate": ["2026-09-30", "2026-08-01", "2026-07-30", "2026-04-20"],
        "items": ["4.02,9.01", "", "2.02,9.01", "5.02"], "accessionNumber": ["a1", "a2", "a3", "a4"]}}}
    filings = sec_8k.parse_recent(payload, TODAY)
    assert [f["accession"] for f in filings] == ["a1", "a3"]             # 10-Q and older than 90 days left out
    assert filings[0]["serious"] and "restatement" in filings[0]["title"]
    assert filings[1]["title"] == "8-K filed 2026-07-30: 2.02 earnings results"
    assert spike.serious_filings(filings) == [filings[0]]


def test_gdelt_timeline_and_spike_rule():
    volume = [{"date": f"d{i}", "value": 1.0} for i in range(30)] + [{"date": f"r{i}", "value": 3.5} for i in range(7)]
    assert spike.coverage_spike(volume) == {"status": "spike", "ratio": 3.5}
    assert spike.coverage_spike([])["status"] == "skipped"
    assert gdelt.parse_timeline({"timeline": [{"data": [{"date": "20261001T000000Z", "value": 0.4}]}]}) == \
        [{"date": "20261001", "value": 0.4}]


# --- Deduplication and sentiment -------------------------------------------------------------------

def test_dedupe_keeps_the_trusted_copy_and_numbers_items():
    items = [{"title": "Jio Platforms plans IPO on October 21", "date": "2026-10-05", "trusted": False, "url": "u1"},
             {"title": "Jio Platforms plans IPO on Oct 21", "date": "2026-10-04", "trusted": True, "url": "u2"},
             {"title": "Reliance raises Rs 12,000 crore in bonds", "date": "2026-09-30", "trusted": True, "url": "u3"}]
    kept = aggregate.dedupe(items)
    assert [k["url"] for k in kept] == ["u2", "u3"] and [k["id"] for k in kept] == ["n1", "n2"]


def test_only_business_threats_are_serious_and_duplicates_count_once():
    base = {"relevant": True, "kind": "fact_report", "sentiment": "negative", "materiality": "high", "date": "2026-09-26"}
    labelled = [{**base, "id": "n1", "title": "Jury says Apple owes $5.7 billion in patent case"},             # routine
                {**base, "id": "n2", "title": "Company files for bankruptcy", "threatens_business": True},
                {**base, "id": "n3", "title": "Same bankruptcy, another outlet", "threatens_business": True,
                 "duplicate_of": "n2"}]
    out = aggregate.summarise(labelled, TODAY)
    assert out["serious_negative_ids"] == ["n2"] and out["relevant_count"] == 2


def test_net_sentiment_weights_and_serious_items():
    labelled = [
        {"id": "n1", "relevant": True, "sentiment": "positive", "materiality": "high", "kind": "fact_report", "date": "2026-10-04"},
        {"id": "n2", "relevant": True, "sentiment": "negative", "materiality": "low", "kind": "opinion", "date": "2026-09-01"},
        {"id": "n3", "relevant": False, "sentiment": "negative", "materiality": "high", "kind": "fact_report", "date": "2026-10-04"},
        {"id": "n4", "relevant": True, "sentiment": "negative", "materiality": "high", "kind": "fact_report", "date": "2026-09-01",
         "threatens_business": True},
    ]
    out = aggregate.summarise(labelled, TODAY)
    # weights: n1 3*1*2 = 6 (+1), n2 1*0.5 = 0.5 (-1), n4 3*1 = 3 (-1); n3 not relevant
    assert out["net_sentiment"] == pytest.approx((6 - 0.5 - 3) / 9.5, abs=1e-4)
    assert out["relevant_count"] == 3 and out["top_ids"][0] == "n1" and out["serious_negative_ids"] == ["n4"]


# --- Server and node ----------------------------------------------------------------------------------

class FakeSources:
    calls: list = []

    @staticmethod
    async def articles(name, market, credits):
        FakeSources.calls.append(("tavily", name, credits))
        return [{"title": "Reliance plans Rs 1 lakh crore Andhra investment", "date": "2026-10-02", "trusted": True,
                 "url": "https://www.moneycontrol.com/x", "outlet": "moneycontrol.com"}], [], 2

    @staticmethod
    async def indian_disclosures(ticker):
        return trendlyne_disclosures.parse_news((FIXTURES / "news_reliance.txt").read_text(encoding="utf-8")), []

    @staticmethod
    async def us_filings(cik):
        raise AssertionError("not called for India")

    @staticmethod
    async def coverage(name):
        return {}, ["gdelt_unavailable"]

    @staticmethod
    def credits():
        return 998


def test_server_gathers_dedupes_and_caches():
    FakeSources.calls = []
    cache = CacheStore(connect(":memory:"))
    out = asyncio.run(server.news_logic("IN", "RELIANCE", "Reliance Industries Limited", cache=cache, src=FakeSources()))
    assert FakeSources.calls[0][1] == "Reliance Industries"                     # legal words dropped for searching
    assert len(out["items"]) >= 2 and out["items"][0]["id"] == "n1" and out["tavily_credits_used"] == 2
    assert out["items"][0]["date"] == "2026-10-05"                               # newest first: the Jio IPO story
    assert "gdelt_unavailable" in out["flags"] and out["spike"]["status"] == "skipped"
    again = asyncio.run(server.news_logic("IN", "RELIANCE", "Reliance Industries Limited", cache=cache, src=FakeSources()))
    assert len(FakeSources.calls) == 1 and "from_cache" in again["flags"]


def test_labels_join_by_id_and_invented_ids_are_ignored():
    items = [{"id": "n1", "title": "a"}, {"id": "n2", "title": "b"}]
    labels = Labels(items=[ItemLabel(id="n1", relevant=True, kind="fact_report", event_type="product",
                                     sentiment="positive", materiality="medium", reason="a"),
                           ItemLabel(id="n9", relevant=True, kind="opinion", event_type="other",
                                     sentiment="negative", materiality="high", reason="made up")])
    out = apply_labels(items, labels)
    assert out[0]["labelled"] and out[0]["sentiment"] == "positive" and out[1]["labelled"] is False


def test_node_labels_scores_and_records():
    raw = {"items": [{"id": "n1", "title": "Jio Platforms plans IPO on October 21", "date": "2026-10-05",
                      "url": "https://reuters.com/x", "origin": "tavily", "outlet": "reuters.com"},
                     {"id": "n2", "title": "Reliance shares fall", "date": "2026-09-11", "url": "https://bs.com/y",
                      "origin": "tavily", "outlet": "business-standard.com"}],
           "filings": [], "flags": [], "as_of": "2026-10-05"}

    async def fetch(market, ticker, name, cik):
        return raw

    async def label(system, items, state):
        assert "Reliance Industries Limited (RELIANCE, NSE)" in system
        return Labels(items=[ItemLabel(id="n1", relevant=True, kind="fact_report", event_type="merger_acquisition",
                                       sentiment="positive", materiality="high", reason="IPO planned"),
                             ItemLabel(id="n2", relevant=False, kind="fact_report", event_type="other",
                                       sentiment="negative", materiality="low", reason="price move only")])

    state = {"ticker": "RELIANCE", "market": "IN", "exchange": "NSE", "company_name": "Reliance Industries Limited"}
    out = asyncio.run(make_news_node(fetch, label, TODAY)(state))
    assert out["news"]["summary"]["net_sentiment"] == 1.0 and out["news"]["summary"]["relevant_count"] == 1
    ids = [r["record_id"] for r in out["news_records"]]
    assert ids == ["tavily:RELIANCE:news_item:n1", "computed:RELIANCE:news_net_sentiment"]


def test_node_keeps_going_when_labelling_fails():
    async def fetch(market, ticker, name, cik):
        return {"items": [{"id": "n1", "title": "x", "origin": "tavily"}], "filings": [], "flags": []}

    async def label(system, items, state):
        raise RuntimeError("model down")

    out = asyncio.run(make_news_node(fetch, label, TODAY)({"ticker": "X", "market": "US"}))
    assert "labelling_failed" in out["news"]["flags"] and out["news"]["summary"]["net_sentiment"] is None


# --- Label memory (A12) -------------------------------------------------------------------------

def _memory_items(order):
    base = {"a": {"title": "Reliance sues food regulator", "url": "https://reuters.com/a", "outlet": "reuters.com"},
            "b": {"title": "Reliance raises bonds", "url": "https://bs.com/b", "outlet": "bs.com"},
            "c": {"title": "Reliance sues regulator over label", "url": "https://mc.com/c", "outlet": "mc.com"}}
    return [{**base[k], "id": f"n{n}", "origin": "tavily", "date": "2026-10-01"} for n, k in enumerate(order, 1)]


def _lab(i, sentiment, dup=None):
    return ItemLabel(id=i, relevant=True, kind="fact_report", event_type="litigation", sentiment=sentiment,
                     materiality="medium", duplicate_of=dup, reason="r")


def test_saved_labels_are_reused_and_win_over_a_new_answer():
    import asyncio

    from invest_agent.agents.news import labels_with_memory
    from invest_agent.cache.store import CacheStore
    from invest_agent.storage.db import connect

    cache = CacheStore(connect(":memory:"))
    calls = []

    def caller(labels):
        async def call(extra):
            calls.append(extra)
            return Labels(items=labels)
        return call

    first = _memory_items(["a", "b"])
    out, called = asyncio.run(labels_with_memory("RELIANCE", first, caller([_lab("n1", "negative"),
                                                                             _lab("n2", "neutral")]), cache))
    assert called and [lab.sentiment for lab in out.items] == ["negative", "neutral"]

    # Same articles, different order: no model call, same labels.
    again = _memory_items(["b", "a"])
    out, called = asyncio.run(labels_with_memory("RELIANCE", again, caller([]), cache))
    assert not called and {lab.id: lab.sentiment for lab in out.items} == {"n1": "neutral", "n2": "negative"}

    # A new article arrives: one call, the model now says "neutral" for the old lawsuit
    # story, but the saved "negative" label wins; the new item's repeat link is kept.
    third = _memory_items(["a", "b", "c"])
    out, called = asyncio.run(labels_with_memory(
        "RELIANCE", third, caller([_lab("n1", "neutral"), _lab("n2", "neutral"), _lab("n3", "negative", "n1")]), cache))
    by_id = {lab.id: lab for lab in out.items}
    assert called and by_id["n1"].sentiment == "negative" and by_id["n3"].duplicate_of == "n1"
    assert len(calls) == 2

    # The repeat link survives a reorder, pointing at the right article's new id.
    out, called = asyncio.run(labels_with_memory("RELIANCE", _memory_items(["c", "a", "b"]), caller([]), cache))
    assert not called and {lab.id: lab.duplicate_of for lab in out.items}["n1"] == "n2"


def test_same_story_from_another_outlet_keeps_its_label():
    import asyncio

    from invest_agent.agents.news import labels_with_memory
    from invest_agent.cache.store import CacheStore
    from invest_agent.storage.db import connect

    cache = CacheStore(connect(":memory:"))
    prompts = []

    def caller(labels):
        async def call(extra):
            prompts.append(extra)
            return Labels(items=labels)
        return call

    # Run 1: Reuters' lawsuit story, labelled negative.
    asyncio.run(labels_with_memory("RELIANCE", _memory_items(["a"]), caller([_lab("n1", "negative")]), cache))
    # Run 2: only Moneycontrol's version of the same story. The model is shown the earlier
    # story, points at it, and even if it says "neutral" the story keeps "negative".
    out, called = asyncio.run(labels_with_memory("RELIANCE", _memory_items(["c"]),
                                                 caller([_lab("n1", "neutral", "p1")]), cache))
    assert called and "p1: Reliance sues food regulator (labelled negative, medium)" in prompts[-1]
    assert out.items[0].sentiment == "negative" and out.items[0].duplicate_of is None
