"""
Smoke test: can our own code connect to Trendlyne's MCP server (first-month check 1).

Lists Trendlyne's tools, which does not use any of the plan's 400 daily tool calls,
and checks the ones the Fundamentals Agent needs are there. The private URL comes
from TRENDLYNE_MCP_URL in .env and is never printed.

Run with: uv run pytest tests/smoke/test_smoke_trendlyne.py -v
"""

import asyncio

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from invest_agent.config import get_settings


@pytest.mark.smoke
def test_trendlyne_tools_are_reachable():
    url = get_settings().trendlyne_mcp_url
    if not url:
        pytest.skip("TRENDLYNE_MCP_URL not set in .env")
    client = MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": url}})
    names = {t.name for t in asyncio.run(client.get_tools())}
    assert {"search_entities", "search_financial_parameters", "get_stock_parameter_values"} <= names
