"""
scripts/news_probe.py: one-off check of the news sources before step 8b is built.

What this file does:
    The News agent (design 14.6) gathers articles and official disclosures about the
    company, labels them with one Luna call, and scores them. Before writing it we look
    at what each source really sends back:

      A. Tavily (key needed)   one company-news search restricted to trusted Indian
                               outlets: uses 1 of the 1,000 free monthly credits
      B. GDELT (no key)        90-day coverage volume and tone timelines for the company,
                               and its article list
      C. SEC (no key)          Apple's recent 8-K filings and their item codes
      D. Trendlyne             its news and corporate events tool and its document search
                               for Reliance (2 calls of the 400 a day)

    It prints a short summary of each. No keys or web addresses are printed.

Run with:
    uv run python scripts/news_probe.py
    uv run python scripts/news_probe.py --skip-tavily     (a rerun that saves the Tavily credit)
"""

import asyncio
import json
import time

import invest_agent  # noqa: F401
import httpx
import yaml

from invest_agent.config import get_settings


def short(text, n=900):
    text = str(text)
    return text[:n] + (" ..." if len(text) > n else "")


def tavily() -> None:
    print("\n--- A. Tavily: 'Reliance Industries company news', trusted Indian outlets, last month")
    key = get_settings().tavily_api_key
    if not key:
        print("skipped: TAVILY_API_KEY not set in .env")
        return
    domains = yaml.safe_load(open("src/invest_agent/mcp_servers/news/trusted_domains.yaml", encoding="utf-8"))["IN"]
    body = {"query": "Reliance Industries company news", "topic": "news", "time_range": "month",
            "max_results": 10, "include_domains": domains}
    r = httpx.post("https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    print("HTTP", r.status_code)
    if r.status_code != 200:
        print(short(r.text, 400))
        return
    data = r.json()
    print("keys:", list(data.keys()))
    for item in data.get("results", [])[:10]:
        print(f"- {item.get('published_date')} | {item.get('url', '')[:90]}\n  {item.get('title')}\n  {short(item.get('content'), 160)}")
    print("other fields in a result:", [k for k in (data.get("results") or [{}])[0].keys()])


def gdelt() -> None:
    # GDELT allows one request every 5 seconds (its 429 answer on 5 October said so), so
    # this waits 6 seconds before each call, and once more after a 429.
    print("\n--- B. GDELT: 90 days of coverage for \"Reliance Industries\"")
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    with httpx.Client(timeout=30) as client:
        for mode in ("timelinevol", "timelinetone", "artlist"):
            params = {"query": '"Reliance Industries"', "mode": mode, "format": "json", "timespan": "90d"}
            if mode == "artlist":
                params["maxrecords"] = 5
            r = None
            for attempt in (1, 2):
                time.sleep(6)
                try:
                    r = client.get(base, params=params)
                except httpx.HTTPError as exc:
                    print(f"[{mode}] attempt {attempt} failed: {type(exc).__name__}")
                    r = None
                    continue
                if r.status_code != 429:
                    break
            if r is None:
                continue
            print(f"[{mode}] HTTP {r.status_code}")
            try:
                data = r.json()
                if mode == "artlist":
                    for a in data.get("articles", [])[:5]:
                        print(f"  {a.get('seendate')} {a.get('domain')} {a.get('language')} | {short(a.get('title'), 100)}")
                else:
                    series = (data.get("timeline") or [{}])[0].get("data", [])
                    print(f"  {len(series)} points; last 3: {series[-3:]}")
            except json.JSONDecodeError:
                print("  not JSON:", short(r.text, 200))


def sec_8k() -> None:
    print("\n--- C. SEC: Apple's recent 8-K filings")
    r = httpx.get("https://data.sec.gov/submissions/CIK0000320193.json",
                  headers={"User-Agent": get_settings().sec_edgar_user_agent}, timeout=30)
    print("HTTP", r.status_code)
    recent = r.json()["filings"]["recent"]
    rows = [(f, d, i, a) for f, d, i, a in zip(recent["form"], recent["filingDate"], recent["items"], recent["accessionNumber"])
            if f == "8-K"][:6]
    for form, filed, items, accession in rows:
        print(f"  {filed} {form} items={items} {accession}")


async def trendlyne() -> None:
    print("\n--- D. Trendlyne: news and corporate events, document search, for RELIANCE")
    if not get_settings().trendlyne_mcp_url:
        print("skipped: TRENDLYNE_MCP_URL not set")
        return
    from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne
    for tool, args in (("get_overview_news_corp_events", {"stock_code": "RELIANCE"}),
                       ("get_document_search_results", {"query": "Reliance Industries quarterly results"})):
        try:
            print(f"[{tool}]", short(await call_trendlyne(tool, args), 1500))
        except Exception as exc:
            print(f"[{tool}] failed:", type(exc).__name__, short(exc, 500))


if __name__ == "__main__":
    import sys
    if "--skip-tavily" not in sys.argv:     # the second run skips Tavily, to save a credit
        tavily()
    gdelt()
    sec_8k()
    asyncio.run(trendlyne())
