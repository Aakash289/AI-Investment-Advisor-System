"""
scripts/news_probe3.py: what Trendlyne's news and insider-trade answers look like (build step 8b).

What this file does:
    news_probe2.py showed the arguments: get_overview_news_corp_events takes a stock code
    and type "news" (news feed and exchange announcements), and
    get_ownership_deals_insider_sast takes type "sast" (insider trades, promoter buying
    and selling, pledges). This makes those two calls for Reliance (2 of the 400 a day),
    prints the start of each answer, and saves both whole as test fixtures:

        tests/fixtures/trendlyne/news_reliance.txt
        tests/fixtures/trendlyne/sast_reliance.txt

Run with:
    uv run python scripts/news_probe3.py
"""

import asyncio
from pathlib import Path

import invest_agent  # noqa: F401

from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne

CALLS = [("get_overview_news_corp_events", "news", "news_reliance.txt"),
         ("get_ownership_deals_insider_sast", "sast", "sast_reliance.txt")]


async def main() -> None:
    folder = Path("tests/fixtures/trendlyne")
    for tool, kind, filename in CALLS:
        text = await call_trendlyne(tool, {"stock_code": "RELIANCE", "type": kind})
        (folder / filename).write_text(text, encoding="utf-8")
        print(f"\n--- {tool} type={kind}: {len(text)} characters (saved to {folder / filename})")
        print(text[:3000])


if __name__ == "__main__":
    asyncio.run(main())
