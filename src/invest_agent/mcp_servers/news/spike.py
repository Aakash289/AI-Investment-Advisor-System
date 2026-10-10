"""
mcp_servers/news/spike.py: the two "something unusual is happening" rules (design 14.6).

What this file does:
    coverage spike   the last 7 days' average coverage is at least three times the 30
                     days before it. Skipped when there is no GDELT timeline, or when the
                     baseline is too thin to mean anything (a company almost nobody writes
                     about can triple from one article to three)
    serious filing   any recent 8-K with item 1.03 (bankruptcy) or 4.02 (restatement)

    The spike rule only flags; the serious-filing rule caps the verdict at neutral later
    (design 14.11). Thresholds are in trusted_domains.yaml (spike_rule).
"""

from __future__ import annotations

from invest_agent.common.yaml_loader import load_yaml


def coverage_spike(volume: list[dict]) -> dict:
    cfg = load_yaml("mcp_servers/news/trusted_domains.yaml")["spike_rule"]
    if len(volume) < 37:
        return {"status": "skipped", "reason": "no coverage timeline" if not volume else "under 37 days of coverage"}
    values = [p["value"] for p in volume]
    recent, baseline = values[-7:], values[-37:-7]
    base_avg = sum(baseline) / len(baseline)
    if base_avg <= 0 or sum(1 for v in baseline if v > 0) < cfg["min_baseline_articles"]:
        return {"status": "skipped", "reason": "too little normal coverage to compare with"}
    ratio = (sum(recent) / len(recent)) / base_avg
    return {"status": "spike" if ratio >= cfg["multiple_of_30_day_average"] else "normal", "ratio": round(ratio, 2)}


def serious_filings(filings: list[dict]) -> list[dict]:
    return [f for f in filings if f.get("serious")]
