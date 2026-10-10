"""
mcp_servers/news/gdelt.py: news coverage volume and tone from GDELT, when it can be reached.

What this file does:
    GDELT watches news worldwide and offers, free and with no key, a 90-day timeline of
    how much coverage a name gets and how positive or negative that coverage is (design
    14.6). It feeds the spike rule (coverage suddenly three times its normal level) and is
    shown as context, never added to the score.

    On 5 October GDELT refused our calls ("one request every 5 seconds") and then timed
    out connecting (build_progress_checklist.md, A7). So it is optional here: one short
    try per timeline, no retries, at most one call every 6 seconds, and any failure
    simply leaves volume and tone out, flagged.

    Speed (7 October): GDELT is still unreachable, and each request waited out a 10-second
    timeout. Now the timeout is 5 seconds, it runs alongside the other news sources, and
    the Source Health Monitor marks it down for 12 hours after 2 failures, so later requests
    skip it at once until it is tried again.
"""

from __future__ import annotations

import httpx

from invest_agent.observability.tracked_call import tracked_call

URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def parse_timeline(payload: dict) -> list[dict]:
    """[{date, value}] from a timelinevol or timelinetone answer."""
    series = (payload.get("timeline") or [{}])[0].get("data", []) if payload else []
    return [{"date": str(p.get("date", ""))[:8], "value": float(p["value"])} for p in series if p.get("value") is not None]


async def _get(name: str, mode: str) -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(URL, params={"query": f'"{name}"', "mode": mode, "format": "json", "timespan": "90d"})
        response.raise_for_status()
        return response.json()


async def coverage(name: str) -> tuple[dict, list[str]]:
    """{"volume": [...], "tone": [...]} and flags. Stops at the first failure."""
    out: dict = {}
    for mode, key in (("timelinevol", "volume"), ("timelinetone", "tone")):
        try:
            out[key] = parse_timeline(await tracked_call("gdelt", mode, _get, name, mode, timeout=6, retries=0))
        except Exception:
            return out, ["gdelt_unavailable"]
    return out, []
