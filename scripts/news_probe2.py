"""
scripts/news_probe2.py: two small follow-ups to news_probe.py (build step 8b).

What this file does:
    1. Trendlyne: prints the full description and arguments of its news, corporate
       events and insider-deals tools. get_overview_news_corp_events refused a call
       without a "type" argument, and the allowed values are in its description.
       Listing tools does not count against the plan's calls.
    2. GDELT: refused every call on 5 October ("one request every 5 seconds"), even with
       6-second gaps, which suggests it had blocked this connection for a while after the
       first burst. This waits a full minute, then makes a single call, to see whether
       GDELT works at all from here.

Run with:
    uv run python scripts/news_probe2.py
"""

import asyncio
import json
import time

import invest_agent  # noqa: F401
import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from invest_agent.config import get_settings

TOOLS = ["get_overview_news_corp_events", "get_ownership_deals_insider_sast", "get_document_search_results"]


async def trendlyne_schemas() -> None:
    print("--- 1. Trendlyne tool descriptions and arguments")
    client = MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": get_settings().trendlyne_mcp_url}})
    async with client.session("trendlyne") as session:
        for tool in await load_mcp_tools(session):
            if tool.name in TOOLS:
                schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.args_schema.model_json_schema()
                print(f"\n[{tool.name}]\n{tool.description}\narguments: {json.dumps(schema.get('properties', {}), indent=1)}"
                      f"\nrequired: {schema.get('required')}")


def gdelt_once() -> None:
    print("\n--- 2. GDELT: one call after a 60-second wait")
    time.sleep(60)
    try:
        r = httpx.get("https://api.gdeltproject.org/api/v2/doc/doc",
                      params={"query": '"Reliance Industries"', "mode": "timelinevol", "format": "json", "timespan": "90d"},
                      timeout=30)
        print("HTTP", r.status_code)
        try:
            series = (r.json().get("timeline") or [{}])[0].get("data", [])
            print(f"{len(series)} points; last 3: {series[-3:]}")
        except json.JSONDecodeError:
            print(r.text[:300])
    except httpx.HTTPError as exc:
        print("failed:", type(exc).__name__)


if __name__ == "__main__":
    asyncio.run(trendlyne_schemas())
    gdelt_once()
