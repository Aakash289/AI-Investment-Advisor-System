"""
mcp_servers/filings/peers_us.py: P/E, P/B, EV/EBITDA and P/S for a US company's peers.

What this file does:
    Decided 5 October after scripts/peer_probe.py (design 14.9, build step 7b):

      peer list     FMP's free stock-peers endpoint (1 call): similar companies with
                    their price and market value. SEC has no free list of companies by
                    industry code, so this replaces the SIC-code peer groups.
      peer figures  SEC "frames": one call returns one figure (say net income for the
                    2025 calendar year) for every US filer, about 6,000 companies. A
                    handful of frames gives every peer's revenue, net income, operating
                    income, depreciation, equity, debt and cash at once. They are official
                    (Tier 1), cached for 24 hours and shared by every company analysed.

    Each figure uses the same tags, in the same order of preference, as the company's
    own figures (xbrl_tag_map.yaml), and the multiples use the same formulas as
    verification/valuation.py, so the company and its peers are measured alike.

    Frames are by calendar year: CY2025 holds each company's annual figures for the
    fiscal year that best fits 2025 (Apple's year to September 2025, for example).
    Balance sheet figures sit in the frame of the quarter their date falls in, so they
    try CY2025Q4I first, then Q3I, Q2I and Q1I for any peer still missing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from invest_agent.cache.store import CacheStore
from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.mcp_servers.entity.reference_data import load_reference_data
from invest_agent.observability.tracked_call import tracked_call

logger = logging.getLogger(__name__)

FMP_PEERS_URL = "https://financialmodelingprep.com/stable/stock-peers"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
FRAME_NAMESPACE = "sec_frame"
FIGURES = ["revenue", "net_income", "operating_income", "depreciation_amortization",
           "total_equity", "long_term_debt", "short_term_debt", "cash"]
# Extra tags tried for peers only, after the company tags, so more peers have EBITDA (A3,
# 6 October: Apple had only 4 peers with a usable EV/EBITDA). A tag nobody used just
# returns an empty frame.
EXTRA_TAGS = {"depreciation_amortization": ["Depreciation", "CostDepreciationAmortizationAndDepletion",
                                            "DepreciationAmortizationAndAccretionNet", "DepreciationAmortizationAndOther"],
              "cash": ["CashCashEquivalentsAndShortTermInvestments", "Cash"]}


def frame_year(today: date | None = None) -> int:
    """The latest calendar year whose annual reports are all filed: from April, last year."""
    today = today or date.today()
    return today.year - 1 if today.month >= 4 else today.year - 2


# --- Calls ---------------------------------------------------------------------------------

async def _get_peers(symbol: str) -> list[dict]:
    key = get_settings().financial_modeling_prep_api_key
    if not key:
        raise RuntimeError("FINANCIAL_MODELING_PREP_API_KEY is not set in .env")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(FMP_PEERS_URL, params={"symbol": symbol, "apikey": key})
        response.raise_for_status()
        return response.json() or []


async def fmp_peers(symbol: str) -> list[dict]:
    """[{symbol, name, price, market_value}] from FMP, without the company itself."""
    rows = await tracked_call("fmp", "stock_peers", _get_peers, symbol, timeout=15)
    return [{"symbol": r["symbol"], "name": r.get("companyName"), "price": r.get("price"),
             "market_value": r.get("mktCap")}
            for r in rows if isinstance(r, dict) and r.get("symbol") and r["symbol"] != symbol]


async def _get_frame(url: str) -> dict:
    async with httpx.AsyncClient(headers={"User-Agent": get_settings().sec_edgar_user_agent},
                                 follow_redirects=True, timeout=60) as client:
        response = await client.get(url)
        if response.status_code == 404:          # no company used this tag in that period
            return {"data": []}
        response.raise_for_status()
        return response.json()


def parse_frame(payload: dict) -> dict[str, float]:
    """{cik as text: value} from a frame answer."""
    return {str(row["cik"]): float(row["val"]) for row in payload.get("data", [])
            if row.get("cik") is not None and row.get("val") is not None}


async def sec_frame(tag: str, unit: str, period: str, taxonomy: str = "us-gaap",
                    cache: CacheStore | None = None) -> dict[str, float]:
    """One frame, cached for 24 hours (only {cik: value} is kept, not the whole answer)."""
    cache = cache or CacheStore()
    key = f"{taxonomy}:{tag}:{unit}:{period}"
    if (hit := cache.get(FRAME_NAMESPACE, key)) is not None:
        return hit
    url = FRAME_URL.format(taxonomy=taxonomy, tag=tag, unit=unit, period=period)
    values = parse_frame(await tracked_call("sec_edgar", "frame", _get_frame, url, timeout=60))
    cache.set(FRAME_NAMESPACE, key, values, ttl=timedelta(hours=24))
    return values


# --- Turning frames into peer multiples --------------------------------------------------------

async def peer_figures(ciks: list[str], year: int, frame_fn=sec_frame) -> dict[str, dict[str, float]]:
    """{cik: {figure: value}} for the peers, trying each figure's tags in order of
    preference and only fetching the next tag's frame if some peers are still missing."""
    tag_map = load_yaml("mcp_servers/filings/xbrl_tag_map.yaml")
    out: dict[str, dict[str, float]] = {c: {} for c in ciks}
    for figure in FIGURES:
        spec = tag_map[figure]
        # A balance sheet date sits in the frame of the quarter it falls in: Apple's year
        # ends in September (CY2025Q3I), Microsoft's in June (Q2I). So instant figures try
        # the year-end quarter first, then the earlier ones, for peers still missing.
        periods = [f"CY{year}Q{q}I" for q in (4, 3, 2, 1)] if spec["kind"] == "instant" else [f"CY{year}"]
        tags = list(spec["tags"]) + [t for t in EXTRA_TAGS.get(figure, []) if t not in spec["tags"]]
        for tag in tags:
            for period in periods:
                missing = [c for c in ciks if figure not in out[c]]
                if not missing:
                    break
                try:
                    frame = await frame_fn(tag, spec["unit"], period, spec.get("taxonomy", "us-gaap"))
                except Exception as err:
                    logger.warning("SEC frame %s %s failed: %s", tag, period, err)
                    continue
                for c in missing:
                    if c in frame:
                        out[c][figure] = frame[c]
    return out


def multiples(market_value: float | None, f: dict[str, float]) -> dict[str, float | None]:
    """The same formulas as verification/valuation.py; None when not meaningful."""
    if not market_value:
        return {}
    net, equity, revenue = f.get("net_income"), f.get("total_equity"), f.get("revenue")
    out: dict[str, float | None] = {
        "pe": market_value / net if net and net > 0 else None,
        "pb": market_value / equity if equity and equity > 0 else None,
        "ps": market_value / revenue if revenue and revenue > 0 else None,
    }
    ebitda = None
    if f.get("operating_income") is not None and f.get("depreciation_amortization") is not None:
        ebitda = f["operating_income"] + f["depreciation_amortization"]
    debt = (f.get("long_term_debt") or 0) + (f.get("short_term_debt") or 0)
    if ebitda and ebitda > 0 and f.get("cash") is not None:
        out["ev_ebitda"] = (market_value + debt - f["cash"]) / ebitda
    else:
        out["ev_ebitda"] = None
    return out


async def us_peer_multiples(ticker: str, today: date | None = None, peers_fn=fmp_peers,
                            frame_fn=sec_frame, ciks_fn=None) -> dict:
    """{peers: [{symbol, name, market_value, pe, pb, ps, ev_ebitda}], basis, flags}."""
    year = frame_year(today)
    peers = await peers_fn(ticker)
    if not peers:
        return {"peers": [], "basis": None, "flags": ["no_peer_list"]}

    if ciks_fn is None:
        ref = await load_reference_data()
        by_ticker = {listing.ticker: str(listing.cik) for listing in ref.listings if listing.cik}
    else:
        by_ticker = ciks_fn()
    company_cik = by_ticker.get(ticker)

    rows, seen = [], {company_cik}
    for p in peers:
        cik = by_ticker.get(p["symbol"])
        if cik is None or cik in seen:          # unknown to SEC, or a second share class
            continue
        seen.add(cik)
        rows.append({**p, "cik": cik})

    figures = await peer_figures([r["cik"] for r in rows], year, frame_fn)
    for r in rows:
        r.update(multiples(r.get("market_value"), figures.get(r["cik"], {})))
        r["figures"] = figures.get(r["cik"], {})
        r["missing_figures"] = [f for f in FIGURES if f not in r["figures"]]
    return {"peers": rows, "basis": f"SEC frames CY{year}; market value from FMP", "year": year, "flags": []}
