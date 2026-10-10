"""
Unit tests for build step 6, the rate side: fx/checks.py, fred_fx.py's parser and the
source order, checks and cache in mcp_servers/fx/server.py.

The sources are fakes, so no network or key is needed. Run with:
    uv run pytest tests/unit/test_fx_checks.py -v
"""

import asyncio
import json
from datetime import date
from pathlib import Path

from invest_agent.cache.store import CacheStore
from invest_agent.fx.checks import business_days_between, disagrees, is_stale
from invest_agent.mcp_servers.fx import fred_fx
from invest_agent.mcp_servers.fx.server import Sources, rate_logic, series_logic
from invest_agent.storage.db import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fx"
MONDAY = date(2026, 10, 5)


# --- Checks -----------------------------------------------------------------------------

def test_business_days_skip_weekends():
    assert business_days_between("2026-10-02", MONDAY) == 1         # Friday to Monday
    assert business_days_between("2026-09-28", MONDAY) == 5
    assert business_days_between(MONDAY, MONDAY) == 0


def test_stale_rule_three_business_days():
    assert not is_stale("2026-10-02", MONDAY)          # Friday's rate on a Monday is normal
    assert not is_stale("2026-09-30", MONDAY)          # 3 business days: still fine
    assert is_stale("2026-09-28", MONDAY)              # FBIL's rate on 5 October: 5 days, stale


def test_disagreement_over_one_percent():
    assert not disagrees(95.80, 95.81)
    assert disagrees(97.0, 95.81)


def test_fred_parser_skips_empty_days_and_reads_the_real_answer():
    real = json.loads((FIXTURES / "fred_dexinus_latest.json").read_text(encoding="utf-8"))["observations"]
    assert fred_fx.parse_latest(real) == {"rate": 95.81, "date": "2026-09-25"}
    assert fred_fx.parse_latest([{"date": "2026-10-02", "value": "."}] + real)["date"] == "2026-09-25"


# --- Source order -----------------------------------------------------------------------

class Fake:
    """Fake sources: each returns its canned answer, or raises if given an exception."""

    def __init__(self, fbil=None, blend=None, fred=None, yahoo=None, on_date=None):
        self.answers = {"FBIL": fbil, None: blend}
        self.fred_answer, self.yahoo_answer, self.on_date = fred, yahoo, on_date
        self.calls = []

    @staticmethod
    def _give(answer):
        if isinstance(answer, Exception) or answer is None:
            raise answer or RuntimeError("down")
        return dict(answer)

    def sources(self):
        async def frankfurter(base, quote, provider=None):
            self.calls.append(("frankfurter", provider))
            return self._give(self.answers[provider])

        async def frankfurter_on(base, quote, day):
            self.calls.append(("frankfurter_on", day))
            return self._give(self.on_date)

        async def fred(base, quote):
            self.calls.append(("fred",))
            return self._give(self.fred_answer)

        async def yahoo(base, quote):
            self.calls.append(("yahoo",))
            return self._give(self.yahoo_answer)

        return Sources(frankfurter, frankfurter_on, fred, yahoo)


def get(fake, base="USD", quote="INR", cache=None):
    cache = cache or CacheStore(connect(":memory:"))
    return asyncio.run(rate_logic(base, quote, cache=cache, today=MONDAY, sources=fake.sources()))


FRESH = {"rate": 96.2, "date": "2026-10-05"}
NO_KEY = fred_fx.FxSourceUnavailable("FRED_API_KEY is not set")


def test_same_currency_needs_no_source():
    fake = Fake()
    assert get(fake, "INR", "INR")["rate"] == 1.0 and fake.calls == []


def test_fresh_fbil_rate_is_used_first():
    out = get(Fake(fbil={"rate": 96.1, "date": "2026-10-02"}, fred=NO_KEY))
    assert (out["source"], out["provider"], out["rate"], out["tier"]) == ("frankfurter", "FBIL", 96.1, 1)
    assert out["cross_check"]["status"] == "skipped"


def test_fbil_behind_falls_back_to_the_official_blend():
    out = get(Fake(fbil={"rate": 95.97, "date": "2026-09-28"}, blend=FRESH, fred=NO_KEY))
    assert out["provider"] == "official blend" and out["rate"] == 96.2
    assert "fbil_rate_behind" in out["flags"] and "fx_rate_stale" not in out["flags"]


def test_frankfurter_down_uses_fred_flagged_lagged_and_stale():
    out = get(Fake(fred={"rate": 95.81, "date": "2026-09-25"}))
    assert out["source"] == "fred" and "fx_lagged_fred_rate" in out["flags"] and "fx_rate_stale" in out["flags"]


def test_last_resort_is_yahoo_and_then_nothing():
    out = get(Fake(fred=NO_KEY, yahoo={"rate": 96.28, "date": "2026-10-05"}))
    assert out["source"] == "yfinance" and out["tier"] == 2 and "fx_unofficial_rate" in out["flags"]

    cache = CacheStore(connect(":memory:"))
    none = get(Fake(fred=NO_KEY), cache=cache)
    assert none["rate"] is None and "fx_unavailable" in none["flags"]
    assert cache.get("fx_rate", "USD:INR") is None          # a failure is never cached


def test_cross_check_with_fred_on_the_same_date():
    agree = get(Fake(blend=FRESH, fred={"rate": 95.81, "date": "2026-09-25"}, on_date={"rate": 95.8, "date": "2026-09-25"}))
    assert agree["cross_check"]["status"] == "agree" and agree["cross_check"]["date"] == "2026-09-25"
    off = get(Fake(blend=FRESH, fred={"rate": 95.81, "date": "2026-09-25"}, on_date={"rate": 97.5, "date": "2026-09-25"}))
    assert off["cross_check"]["status"] == "disagree" and "fx_sources_disagree" in off["flags"]


def test_pairs_without_the_rupee_skip_fbil_and_fred():
    fake = Fake(blend={"rate": 0.88865, "date": "2026-10-05"})
    out = get(fake, "USD", "EUR")
    assert out["rate"] == 0.88865 and out["cross_check"]["status"] == "not_applicable"
    assert fake.calls == [("frankfurter", None)]


def test_rate_is_cached_for_the_next_request():
    cache = CacheStore(connect(":memory:"))
    get(Fake(blend=FRESH, fred=NO_KEY), cache=cache)
    again = Fake()                                            # every source would fail now
    out = get(again, cache=cache)
    assert out["rate"] == 96.2 and "from_cache" in out["flags"] and again.calls == []


def test_series_failure_is_flagged_not_raised():
    async def broken(*args):
        raise RuntimeError("down")

    out = asyncio.run(series_logic("USD", "INR", cache=CacheStore(connect(":memory:")), today=MONDAY, fetch=broken))
    assert out["points"] == [] and out["flags"] == ["fx_series_unavailable"]
