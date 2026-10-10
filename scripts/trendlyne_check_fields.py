"""
scripts/trendlyne_check_fields.py: Trendlyne first-month checks 2 and 3.

What this file does:
    Asks Trendlyne which parameter codes exist for every raw figure the Fundamentals
    Agent needs (design 14.3), including historical versions ("revenue 3 years ago"),
    and confirms Reliance Industries' stock code. Everything is saved to
    data/trendlyne_fields.txt for Claude to read and pick the right codes from.

    Uses about 19 of the plan's 400 daily tool calls (one per search). The private
    MCP URL is never printed or saved.

Run with:
    uv run python scripts/trendlyne_check_fields.py
"""

import asyncio
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from invest_agent.config import get_settings

# One search per raw figure in design 14.3, plus three that test for history.
QUERIES = [
    "revenue annual", "net profit annual", "operating profit annual", "EBITDA annual",
    "EPS annual", "interest expense annual", "tax expense annual", "depreciation annual",
    "cash from operating activities annual", "capital expenditure", "dividend paid",
    "total equity", "total debt", "cash and equivalents", "shares outstanding",
    "revenue 3 years ago", "net profit 5 years ago", "revenue growth 5 years",
]


async def main() -> None:
    url = get_settings().trendlyne_mcp_url
    client = MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": url}})
    tools = {t.name: t for t in await client.get_tools()}

    lines = []
    # 1 call: confirm the company's codes, sector and industry.
    result = await tools["search_entities"].ainvoke({"query": "Reliance Industries", "limit": 3})
    lines += ["=== search_entities: Reliance Industries", str(result), ""]

    # 18 calls: one per figure.
    for q in QUERIES:
        result = await tools["search_financial_parameters"].ainvoke({"query": q})
        lines += [f"=== search_financial_parameters: {q}", str(result), ""]
        print(f"done: {q}")

    out = Path("data/trendlyne_fields.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved to {out} ({1 + len(QUERIES)} tool calls used)")


if __name__ == "__main__":
    asyncio.run(main())