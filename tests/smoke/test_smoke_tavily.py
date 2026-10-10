"""
Smoke test: Tavily, the news article source (build step 8b). Uses 1 of the 1,000 free
monthly credits, so it is skipped unless TAVILY_API_KEY is set.

Run with: uv run pytest tests/smoke/test_smoke_tavily.py -v
"""

import asyncio

import pytest

from invest_agent.config import get_settings
from invest_agent.mcp_servers.news import tavily


@pytest.mark.smoke
def test_tavily_company_news():
    if not get_settings().tavily_api_key:
        pytest.skip("TAVILY_API_KEY not set in .env")
    items = asyncio.run(tavily.search("Reliance Industries", "IN", "company", ["reuters.com", "economictimes.indiatimes.com"]))
    assert items and all(i["title"] and i["url"] for i in items)
