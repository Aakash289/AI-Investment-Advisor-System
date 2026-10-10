"""
mcp_servers/filings/peers_india.py: P/E, P/B and EV/EBITDA for an Indian company and its peers.

What this file does:
    Decided 5 October after scripts/peer_probe.py and peer_probe_india.py (build step 7b):

      peer list     Trendlyne's search_entities, asked for the company's industry name
                    ("Refineries/Petro-Products"), keeping only rows in exactly that
                    industry. The search matches words, so it also returns companies from
                    other industries ("Ashoka Refineries", a trading company); those are
                    dropped.
      figures       get_stock_parameter_values for the company and its peers together
                    (up to 10 stocks a call): P/E (pettm), P/B (pbva), EV/EBITDA
                    (evperebitdaa) and market value (mcapq, ₹ crore).

    The company's own values come from the same call, so the company and its peers are
    measured alike (Trendlyne's P/E is on the last 12 months, so it differs a little from
    the annual P/E worked out in verification/valuation.py).

    Calls: 1 search plus 1 or 2 value calls (up to 19 peers), cached for 24 hours by the
    server.

    Small industries (scorecard version 6, 10 October): ITC's industry, Cigarettes-Tobacco
    Products, holds only 4 other companies, so it never had a peer median. When the
    industry gives fewer than WIDEN_BELOW peers, the company's sector is searched as well
    (Trendlyne's search table carries each company's sector; ITC's is "Food, Beverages &
    Tobacco", which returned 49 companies) and its companies are added after the industry's
    own, up to MAX_PEERS_WIDENED. The result says it was widened ("widened_to"), so the
    analysis counts the valuation at half weight and says why. Codes that are only numbers
    (BSE-only listings) are left out, as they are not NSE symbols. One more search call.

    A multi-stock answer looks like (real, 5 October):
        PBV Adjusted
        RELIANCE:1.70
        BPCL:1.33
        ---
        PE TTM
        RELIANCE:21.15
        BPCL:7.61
"""

from __future__ import annotations

import json
import re

from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne

CODES = ["pettm", "pbva", "evperebitdaa", "mcapq"]
MAX_STOCKS_PER_CALL = 10
MAX_PEERS = 19
WIDEN_BELOW = 8                 # fewer industry peers than this: add the sector's companies
MAX_PEERS_WIDENED = 29          # 3 value calls at most

# Answer labels to our names. Matched loosely, since Trendlyne writes labels in words.
LABELS = [(re.compile(r"\bpe\b.*ttm|ttm.*\bpe\b"), "pe"),
          (re.compile(r"\bpbv\b|price to book"), "pb"),
          (re.compile(r"\bev\b.*ebitda"), "ev_ebitda"),
          (re.compile(r"market cap"), "market_value_crore")]


def parse_rows(text: str) -> list[dict]:
    """Every row of search_entities' table, as {column: value}."""
    lines = text.replace("\\n", "\n").splitlines()
    header = next((ln for ln in lines if "nse_code" in ln and "|" in ln), None)
    if header is None:
        return []
    cols = [c.strip() for c in header.split("|")]
    rows = []
    for ln in lines[lines.index(header) + 1:]:
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) == len(cols):
            rows.append(dict(zip(cols, cells)))
    return rows


def _sector_of(row: dict) -> str:
    return next((v for k, v in row.items() if "sector" in k.lower()), "") or ""


def _nse(code: str) -> bool:
    """An NSE symbol, not a BSE-only number such as 11330381."""
    return bool(code) and not code.isdigit()


def parse_industry_rows(text: str, industry: str) -> list[dict]:
    """Rows of search_entities' table whose industry is exactly the company's."""
    lines = text.replace("\\n", "\n").splitlines()
    header = next((ln for ln in lines if "nse_code" in ln and "|" in ln), None)
    if header is None:
        return []
    cols = [c.strip() for c in header.split("|")]
    rows = []
    for ln in lines[lines.index(header) + 1:]:
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) == len(cols):
            row = dict(zip(cols, cells))
            if row.get("industry", "").lower() == industry.lower() and row.get("nse_code"):
                rows.append(row)
    return rows


def _name_for(label: str) -> str | None:
    text = label.lower()
    return next((name for pattern, name in LABELS if pattern.search(text)), None)


def parse_multi_values(text: str) -> dict[str, dict[str, float]]:
    """{stock code: {our name: value}} from a get_stock_parameter_values answer for
    several stocks. Unknown labels are skipped; "None" values stay missing."""
    payload = json.loads(text)
    if payload.get("status") != "success":
        raise RuntimeError(f"Trendlyne error: {payload.get('message')}")
    out: dict[str, dict[str, float]] = {}
    for block in str(payload["data"]).split("---"):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        name = None
        for ln in lines:
            if "|" in ln:                          # the header rows listing the stocks
                continue
            m = re.match(r"^([A-Z0-9&\-]+):(.*)$", ln)
            if m and name:
                raw = m.group(2).strip()
                if raw not in ("", "None", "null"):
                    out.setdefault(m.group(1), {})[name] = float(raw)
            elif not m:
                name = _name_for(ln)              # a label line starts a new figure
    return out


async def india_peer_multiples(ticker: str, industry: str | None, call=call_trendlyne) -> dict:
    """{company: {pe, pb, ...}, peers: [{symbol, name, pe, pb, ev_ebitda, market_value}], basis, flags}."""
    if not industry:
        return {"company": {}, "peers": [], "basis": None, "flags": ["no_industry"]}
    text = await call("search_entities", {"query": industry, "limit": 50})
    rows = parse_industry_rows(text, industry)
    peers = [r for r in rows if r["nse_code"].upper() != ticker.upper() and _nse(r["nse_code"])][:MAX_PEERS]
    widened_to, flags = None, []
    if len(peers) < WIDEN_BELOW:
        # The company's own row carries its sector; failing that, the industry rows' sector.
        all_rows = parse_rows(text)
        own = next((r for r in all_rows if r.get("nse_code", "").upper() == ticker.upper()), None)
        sector = _sector_of(own) if own else next((_sector_of(r) for r in rows if _sector_of(r)), "")
        if sector:
            try:
                sector_rows = parse_rows(await call("search_entities", {"query": sector, "limit": 50}))
            except Exception:                       # noqa: BLE001 - the industry peers still stand
                sector_rows = []
                flags.append("sector_search_failed")
            have = {p["nse_code"].upper() for p in peers} | {ticker.upper()}
            extra = [r for r in sector_rows if _sector_of(r).lower() == sector.lower()
                     and _nse(r.get("nse_code", "")) and r["nse_code"].upper() not in have]
            if extra:
                peers = (peers + extra)[:MAX_PEERS_WIDENED]
                widened_to = sector
                flags.append("widened_to_sector")
    if not peers:
        return {"company": {}, "peers": [], "basis": None, "flags": ["no_peer_list"]}

    codes = [ticker] + [p["nse_code"] for p in peers]
    values: dict[str, dict[str, float]] = {}
    for i in range(0, len(codes), MAX_STOCKS_PER_CALL):
        text = await call("get_stock_parameter_values", {"stock_codes": codes[i:i + MAX_STOCKS_PER_CALL],
                                                         "parameters": CODES})
        values.update(parse_multi_values(text))

    def row(code: str, name: str | None) -> dict:
        v = values.get(code, {})
        mv = v.get("market_value_crore")
        return {"symbol": code, "name": name, "pe": v.get("pe"), "pb": v.get("pb"),
                "ev_ebitda": v.get("ev_ebitda"), "market_value": mv * 1e7 if mv is not None else None}

    basis = "Trendlyne: P/E on the last 12 months, P/B, EV/EBITDA (annual)"
    if widened_to:
        basis += f"; peers from the {widened_to} sector, as the {industry} industry has too few"
    return {"company": row(ticker, None), "peers": [row(p["nse_code"], p.get("name")) for p in peers],
            "basis": basis, "flags": flags, "widened_to": widened_to}
