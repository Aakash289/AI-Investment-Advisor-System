"""
verification/peers.py: the company's multiples against the median of its peers (design 14.9, step 7b).

What this file does:
    Takes the peer multiples from filings_server's get_peer_multiples and the company's
    own multiples, and for each multiple works out:

      the peer median     the middle value, which a single giant or tiny peer cannot drag
      the premium         company / median - 1: +25% means 25% dearer than its peers,
                          -25% means 25% cheaper

    Which peers count (A1 and A2, decided 6 October; peer_rules.yaml): US peers without
    any SEC figures (20-F filers such as Sony and TSMC) are left out first; then peers
    smaller than 2% of the company's market value, or below $2 billion or ₹5,000 crore,
    are left out, keeping at least the five largest. Left-out peers are listed with why.

    Rules (design 14.9):
      a median needs at least 5 peers with a meaningful value; with 3 or 4 it is still
      worked out but marked few_peers (scorecard version 6, 10 October: Ford, ITC and
      Nestle India had no valuation at all), and the analysis counts it at half weight;
      with fewer than 3 it is not computed and the reason is given
      a loss-making peer (P/E at or below zero) or negative EBITDA or equity is left out
      of that multiple, never counted as a negative number
      banks and insurers use P/B and P/E only, never EV/EBITDA

    Which company figures are compared: for US companies the multiples from
    verification/valuation.py, built from the same kind of annual figures as the peers'
    SEC frames; for Indian companies Trendlyne's values for the company from the same
    call as its peers, so both sides use the same source and the same period.

    Each median becomes a "computed" record listing the peers it came from, so the
    grounding guardrail can trace it.
"""

from __future__ import annotations

from statistics import median

from invest_agent.common.records import make_record
from invest_agent.common.yaml_loader import load_yaml

MIN_PEERS = 5
MIN_PEERS_LOW_RELIABILITY = 3      # 3 or 4 peers: a median marked few_peers (version 6)
NAMES = {"pe": "P/E", "pb": "P/B", "ev_ebitda": "EV/EBITDA", "ps": "P/S"}


def which_multiples(market: str | None, bank_mode: bool) -> list[str]:
    if bank_mode:
        return ["pb", "pe"]
    return ["pe", "ev_ebitda", "pb", "ps"] if market == "US" else ["pe", "ev_ebitda", "pb"]


def size_filter(peers: list[dict], company_value: float | None, currency: str | None) -> tuple[list[dict], list[dict]]:
    """Keep peers big enough to compare with (peer_rules.yaml). Returns (kept, excluded).
    A peer with no market value is kept: there is nothing to judge it by."""
    rules = load_yaml("verification/peer_rules.yaml")
    floor = max(rules["absolute_floor"].get(currency or "", 0), (company_value or 0) * rules["share_of_company"])
    sized = sorted((p for p in peers if p.get("market_value")), key=lambda p: -p["market_value"])
    big = [p for p in sized if p["market_value"] >= floor]
    if len(big) < rules["min_peers"]:
        big = sized[:rules["min_peers"]]                  # keep the largest rather than too few
    kept_ids = {id(p) for p in big} | {id(p) for p in peers if not p.get("market_value")}
    kept = [p for p in peers if id(p) in kept_ids]
    excluded = [{"symbol": p["symbol"], "reason": "too small to compare with"} for p in peers if id(p) not in kept_ids]
    return kept, excluded


def compare(own: dict, peer_result: dict, *, ticker: str, market: str | None, bank_mode: bool,
            as_of: str | None = None, year: int | None = None, company_value: float | None = None,
            currency: str | None = None) -> dict:
    """Returns {status, basis, peer_count, peers, excluded, comparisons, not_computed, records}."""
    # Peers without any figures (US: filers SEC frames do not cover, such as 20-F filers) go first (A2).
    listed = peer_result.get("peers") or []
    no_figures = [{"symbol": p["symbol"], "reason": "no SEC figures (likely files 20-F)"}
                  for p in listed if market == "US" and not p.get("figures")]
    usable = [p for p in listed if not (market == "US" and not p.get("figures"))]
    peers, too_small = size_filter(usable, company_value, currency)        # A1
    out = {"status": "ok", "basis": peer_result.get("basis"), "peer_count": len(peers),
           "peers": [p["symbol"] for p in peers], "excluded": no_figures + too_small,
           "comparisons": {}, "not_computed": {}, "records": [],
           "missing_figures": {p["symbol"]: p["missing_figures"] for p in peers if p.get("missing_figures")},
           "flags": list(peer_result.get("flags") or []), "widened_to": peer_result.get("widened_to")}
    if not peers:
        out["status"] = "no_peers"
        out["note"] = "No peer list could be found, so there is no peer comparison."
        return out

    company = peer_result.get("company") if market == "IN" else own
    company = company or {}
    for m in which_multiples(market, bank_mode):
        values = [p[m] for p in peers if isinstance(p.get(m), (int, float)) and p[m] > 0]
        if len(values) < MIN_PEERS_LOW_RELIABILITY:
            out["not_computed"][m] = (f"only {len(values)} peers with a meaningful {NAMES[m]} "
                                      f"(at least {MIN_PEERS_LOW_RELIABILITY} needed)")
            continue
        mid = median(values)
        mine = company.get(m)
        entry = {"median": mid, "peers_used": len(values), "company": mine,
                 "premium": (mine / mid - 1) if isinstance(mine, (int, float)) and mine > 0 else None,
                 "few_peers": len(values) < MIN_PEERS}
        out["comparisons"][m] = entry
        used = [p["symbol"] for p in peers if isinstance(p.get(m), (int, float)) and p[m] > 0]
        out["records"].append(make_record(
            source="computed", tier=2, entity=ticker, field=f"peer_median_{m}", value=round(mid, 6), as_of=as_of,
            period=f"FY{year}" if year else None, fiscal_year=year, unit="ratio",
            source_ref=f"{out['basis']}; peers: {', '.join(used)}"))
    if not out["comparisons"]:
        out["status"] = "too_few_peers"
    return out
