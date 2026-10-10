"""
mcp_servers/news/tavily.py: recent articles about the company from Tavily (design 14.6).

What this file does:
    Tavily is a search engine for programs (free plan: 1,000 credits a month, no card;
    a basic search costs 1 credit). It is steered by the query text and its settings,
    not by instructions. Two searches per company:

      company news   "{name} company news"
      risk events    "{name} regulatory action, litigation, management change, downgrade"

    Both look at the last month, news only, and only the trusted outlets for the
    company's market (trusted_domains.yaml). If a search finds fewer than 3 articles, it
    is asked once more without the outlet list, and those results are marked untrusted.

    The probe on 5 October showed that some results are share price or company pages
    ("RIL Share Price Today"), not articles; those are dropped.

    Credit guard: with fewer than 100 credits left this month, the risk-events search is
    skipped; with none left, Tavily is not called at all (design 14.16).
"""

from __future__ import annotations

import asyncio
import re
from email.utils import parsedate_to_datetime

import httpx

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.observability.tracked_call import tracked_call

URL = "https://api.tavily.com/search"
QUERIES = {"company": "{name} company news",
           "risk": "{name} regulatory action, litigation, management change, downgrade"}
NOT_ARTICLES = re.compile(r"share price|stock price|price live|latest news on|announcement, latest news", re.I)
MIN_RESULTS = 3


def is_article(result: dict) -> bool:
    """False for share price and company pages, which are not news articles."""
    return not NOT_ARTICLES.search(result.get("title") or "") and "companyid-" not in (result.get("url") or "")


def iso_date(text: str | None) -> str | None:
    """Tavily writes dates like "Tue, 29 Sep 2026 09:00:00 GMT"; turn that into 2026-09-29."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return str(text)[:10]


def outlet(url: str) -> str:
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0]
    return host


def to_items(results: list[dict], search: str, trusted: bool) -> list[dict]:
    items = []
    for r in results:
        if not is_article(r):
            continue
        items.append({"title": (r.get("title") or "").strip(), "snippet": (r.get("content") or "").strip()[:500],
                      "date": iso_date(r.get("published_date")), "url": r.get("url"), "outlet": outlet(r.get("url")),
                      "origin": "tavily", "search": search, "trusted": trusted})
    return items


async def _post(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(URL, json=body, headers={"Authorization": f"Bearer {get_settings().tavily_api_key}"})
        response.raise_for_status()
        return response.json()


async def search(name: str, market: str, kind: str, domains: list[str] | None) -> list[dict]:
    body = {"query": QUERIES[kind].format(name=name), "topic": "news", "time_range": "month", "max_results": 10}
    if domains:
        body["include_domains"] = domains
    data = await tracked_call("tavily", f"search_{kind}", _post, body, quota_units=1, timeout=30)
    return to_items(data.get("results") or [], kind, trusted=bool(domains))


async def company_articles(name: str, market: str, credits_left: int) -> tuple[list[dict], list[str], int]:
    """Returns (articles, flags, credits used)."""
    if not get_settings().tavily_api_key:
        return [], ["tavily_not_configured"], 0
    trusted = load_yaml("mcp_servers/news/trusted_domains.yaml")
    domains = list(trusted.get(market, [])) + list(trusted.get("press_release_wires", []))
    kinds = ["company", "risk"] if credits_left >= 100 else (["company"] if credits_left > 0 else [])
    flags = [] if len(kinds) == 2 else ["tavily_credits_low" if kinds else "tavily_credits_used_up"]
    # The searches run at the same time (speed, 7 October); each is widened on its own if it
    # found too little.
    async def one(kind: str, spare: int) -> tuple[list[dict], int, bool]:
        found = await search(name, market, kind, domains)
        if len(found) < MIN_RESULTS and spare > 0:
            return found + await search(name, market, kind, None), 2, True     # widened, marked untrusted
        return found, 1, False

    spare = max(0, credits_left - len(kinds))
    results = await asyncio.gather(*(one(kind, spare - i) for i, kind in enumerate(kinds)))
    items, used = [], 0
    for kind, (found, n, widened) in zip(kinds, results):
        items += found
        used += n
        if widened:
            flags.append(f"tavily_{kind}_widened")
    return items, flags, used
