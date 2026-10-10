"""
tests/unit/test_speed.py: the speed changes of 7 October 2026.

What this file checks:
    news sources at once    Tavily, the disclosures and GDELT are asked at the same time,
                            so the news step takes as long as the slowest, not the sum
    Tavily                  its two searches run at once; a thin one is widened on its own
    Trendlyne               news and insider trades asked at once; one failing keeps the other
    labelling cap           at most 15 items: official disclosures first, then trusted
                            outlets, newest first, in their original order
    reasoning effort        the quick Luna calls ask for low effort, and a model that refuses
                            the setting is called again without it (and not asked again)

No network and no model.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from invest_agent.agents import news as news_agent
from invest_agent.cache.store import CacheStore
from invest_agent.common import models
from invest_agent.mcp_servers.news import server, tavily, trendlyne_disclosures
from invest_agent.storage.db import connect


def test_news_sources_run_at_the_same_time():
    class Slow(server.Sources):
        @staticmethod
        async def articles(name, market, credits):
            await asyncio.sleep(0.3)
            return [{"title": "a", "snippet": "", "url": "u1"}], [], 2

        @staticmethod
        async def indian_disclosures(ticker):
            await asyncio.sleep(0.3)
            return [{"title": "b", "snippet": "", "url": "u2"}], []

        @staticmethod
        async def coverage(name):
            await asyncio.sleep(0.3)
            return {}, ["gdelt_unavailable"]

        @staticmethod
        def credits():
            return 500

    t0 = time.perf_counter()
    out = asyncio.run(server.news_logic("IN", "TCS", "Tata Consultancy Services Limited",
                                        cache=CacheStore(connect(":memory:")), src=Slow()))
    assert time.perf_counter() - t0 < 0.6                      # about 0.3 s, not 0.9 s
    assert len(out["items"]) == 2 and out["tavily_credits_used"] == 2 and "gdelt_unavailable" in out["flags"]


def test_tavily_searches_at_once_and_widens_each_on_its_own(monkeypatch):
    calls = []

    async def fake_search(name, market, kind, domains):
        calls.append((kind, bool(domains)))
        await asyncio.sleep(0.2)
        n = 5 if kind == "company" or not domains else 1          # the trusted risk search is thin
        return [{"title": f"{kind}{i}"} for i in range(n)]

    monkeypatch.setattr(tavily, "search", fake_search)
    monkeypatch.setattr(tavily, "get_settings", lambda: type("S", (), {"tavily_api_key": "x"})())
    t0 = time.perf_counter()
    items, flags, used = asyncio.run(tavily.company_articles("TCS", "IN", 500))
    assert time.perf_counter() - t0 < 0.55                     # 2 rounds of 0.2 s at most, not 3
    assert used == 3 and flags == ["tavily_risk_widened"] and len(items) == 5 + 1 + 5


def test_trendlyne_news_and_insider_at_once():
    async def call(tool, args):
        await asyncio.sleep(0.2)
        if tool == "get_ownership_deals_insider_sast":
            raise RuntimeError("down")
        return "no table here"

    t0 = time.perf_counter()
    _items, flags = asyncio.run(trendlyne_disclosures.disclosures("TCS", call=call))
    assert time.perf_counter() - t0 < 0.35
    assert flags == ["trendlyne_insider_unavailable"]


def test_labelling_keeps_the_fifteen_most_useful():
    items = [{"id": f"t{i}", "origin": "tavily", "trusted": False, "date": f"2026-09-{i + 1:02d}"} for i in range(10)]
    items += [{"id": f"s{i}", "origin": "tavily", "trusted": True, "date": f"2026-08-{i + 1:02d}"} for i in range(10)]
    items += [{"id": "ins", "origin": "trendlyne_insider", "trusted": True, "date": "2026-07-01"}]
    kept = news_agent.for_labelling(items)
    ids = [i["id"] for i in kept]
    assert len(kept) == news_agent.MAX_LABELLED and "ins" in ids
    assert all(f"s{i}" in ids for i in range(10))               # every trusted article
    assert ids[:4] == ["t6", "t7", "t8", "t9"]                  # then the newest untrusted, original order


def test_low_effort_with_fallback(monkeypatch):
    seen = []

    class FakeModel:
        def __init__(self, effort):
            self.effort = effort

        async def ainvoke(self, messages):
            seen.append(self.effort)
            if self.effort:
                raise ValueError("Unsupported parameter: 'reasoning_effort' is not supported with this model.")
            return "answer"

    monkeypatch.setattr(models, "_model", lambda name, schema, effort, timeout: FakeModel(effort))
    monkeypatch.setattr(models, "_effort_refused", set())
    assert asyncio.run(models.structured_call("luna", object, [], effort="low")) == "answer"
    assert seen == ["low", None]
    assert asyncio.run(models.structured_call("luna", object, [], effort="low")) == "answer"
    assert seen == ["low", None, None]                          # not asked again in this process


def test_other_errors_are_not_retried(monkeypatch):
    class Broken:
        async def ainvoke(self, messages):
            raise TimeoutError("slow")

    monkeypatch.setattr(models, "_model", lambda *a: Broken())
    monkeypatch.setattr(models, "_effort_refused", set())
    with pytest.raises(TimeoutError):
        asyncio.run(models.structured_call("luna", object, [], effort="low"))
