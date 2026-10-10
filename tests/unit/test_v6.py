"""
tests/unit/test_v6.py: scorecard version 6 (10 October 2026).

What this file checks:
    smooth bands     the score never jumps: tiny steps in the premium give tiny steps in
                     the score; the middle of each band gives the band's score; the open
                     ends reach their score at their edge
    few peers        a median of 3 or 4 peers is worked out and marked few_peers; 2 is not
    caveats          widened or few-peer medians make valuation and forecast low reliability,
                     with the reason, and lower confidence one step
    near the line    a note within 0.1 of either line; none further away or when capped
"""

from __future__ import annotations

import pytest

from invest_agent.analysis import scorecard
from invest_agent.analysis.scoring_common import band, smooth_band
from invest_agent.analysis.step import decide, peer_caveats
from invest_agent.verification import peers

from .test_scorecard import records

LARGE = [[0.50, -2], [0.20, -1], [-0.10, 0], [-0.30, 1], [None, 2]]


def test_smooth_band_has_no_jumps():
    values = [i / 1000 for i in range(-600, 801)]
    scores = [smooth_band(v, LARGE) for v in values]
    assert max(abs(a - b) for a, b in zip(scores, scores[1:])) < 0.02       # never a jump
    assert smooth_band(0.05, LARGE) == 0 and smooth_band(0.35, LARGE) == -1    # middles keep their score
    assert smooth_band(0.20, LARGE) == -0.5                                   # Microsoft's edge: halfway
    assert smooth_band(0.60, LARGE) == -2 and smooth_band(-0.50, LARGE) == 2
    # The step bands are unchanged for the scores that stay stepped.
    assert band(0.20, LARGE) == -1 and band(0.19, LARGE) == 0


def _peer(symbol, pe):
    return {"symbol": symbol, "pe": pe, "pb": None, "ev_ebitda": None, "market_value": 1e12}


def test_three_or_four_peers_give_a_marked_median():
    result = {"peers": [_peer("A", 10.0), _peer("B", 12.0), _peer("C", 14.0)], "company": {"pe": 15.0},
              "widened_to": "Food, Beverages & Tobacco"}
    out = peers.compare({}, result, ticker="ITC", market="IN", bank_mode=False, company_value=1e12, currency="INR")
    c = out["comparisons"]["pe"]
    assert c["median"] == 12.0 and c["few_peers"] is True and out["widened_to"] == "Food, Beverages & Tobacco"
    two = {"peers": [_peer("A", 10.0), _peer("B", 12.0)], "company": {"pe": 15.0}}
    out2 = peers.compare({}, two, ticker="X", market="IN", bank_mode=False, company_value=1e12, currency="INR")
    assert "pe" not in out2["comparisons"] and "at least 3 needed" in out2["not_computed"]["pe"]


def test_caveats_make_valuation_and_forecast_low_reliability():
    val = {"peers": {"widened_to": "Food, Beverages & Tobacco",
                     "comparisons": {"pe": {"premium": 0.3, "peers_used": 4, "few_peers": True}}}}
    why = peer_caveats(val)
    assert "wider Food, Beverages & Tobacco sector" in why["valuation"] and "only 4 peers" in why["valuation"]
    subs = {"business_quality": 1.45, "valuation": -0.6, "macro_fit": 0.5, "news": 0.6, "forecast": 0.3}
    st = {"verified_records": records(), "user_profile": {"goal": "growth", "horizon_years": 10}, "valuation": val}
    card = decide(st, subs)["scorecard"]
    assert card["low_reliability"] == ["valuation", "forecast"]
    assert "sector" in scorecard.reliability_reason(card, "forecast")
    assert any("low reliability (peers from the wider" in r for r in card["confidence"]["reasons"])
    assert peer_caveats({"peers": {"comparisons": {"pe": {"peers_used": 7}}}}) == {}


@pytest.mark.parametrize("score,expected", [(0.74, "favourable line (+0.75)"), (0.80, "favourable line"),
                                            (-0.45, "unfavourable line (-0.5)"), (0.30, None), (-0.70, None)])
def test_near_the_line(score, expected):
    note = scorecard.near_line({"status": "ok", "score": score})
    assert (note is None) if expected is None else (expected in note)


def test_no_near_line_note_when_capped():
    assert scorecard.near_line({"status": "ok", "score": 0.74, "capped": True}) is None


def test_company_row_found_by_name_when_the_code_misses(monkeypatch):
    import asyncio

    from invest_agent.mcp_servers.filings import trendlyne

    header = "name | nse_code | sector | industry"
    by_code = f"status: success\ndata:\n  {header}\n  Nestle Inc ADR | NSRGY | Food | Packaged Foods\n"
    by_name = (f"status: success\ndata:\n  {header}\n"
               "  Nestle India Ltd. | NESTLEIND | Food, Beverages & Tobacco | Packaged Foods\n")
    queries = []

    async def fake_call(tool, args):
        if tool == "search_entities":
            queries.append(args["query"])
            return by_code if args["query"] == "NESTLEIND" else by_name
        return '{"status": "success", "data": ""}'

    async def no_values(code, codes):
        return '{"status": "success", "data": ""}'

    monkeypatch.setattr(trendlyne, "call_trendlyne", fake_call)
    monkeypatch.setattr(trendlyne, "fetch_values", no_values)
    out = asyncio.run(trendlyne.fetch_indian_fundamentals("NESTLEIND", "Nestle India Limited"))
    assert queries == ["NESTLEIND", "Nestle India"]
    assert out["industry"] == "Packaged Foods" and out["sector"] == "Food, Beverages & Tobacco"
    assert "entity_found_by_name" in out["flags"]
