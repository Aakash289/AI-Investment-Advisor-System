"""
scripts/mospi_probe2.py: the second round of the MoSPI code hunt (build step 8a).

What this file does:
    The first round (mospi_probe.py, 5 October) found that MoSPI's upstream service was
    returning errors for some calls, and that CPI and IIP need a base year before they
    list their indicators. This round asks:

      1. get_indicators CPI, base year 2024 (the series running from 2026)
      2. get_indicators IIP, base year 2011-12, monthly
      3. get_indicators PLFS again, quarterly and annual (it failed last time)
      4. get_metadata NAS indicator 22 (GDP growth rate), base year 2022-23, quarterly

    Each call is tried twice, a few seconds apart, since the errors looked temporary.
    Full answers are saved to scripts/mospi_probe2_output.txt. MoSPI needs no key.

Run with:
    uv run python scripts/mospi_probe2.py
"""

import asyncio
from pathlib import Path

import invest_agent  # noqa: F401
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from invest_agent.config import get_settings
from invest_agent.mcp_servers.filings.trendlyne import tool_text

CALLS = [
    ("get_indicators", {"dataset": "CPI", "base_year": "2024", "user_query": "all India general index inflation"}),
    ("get_indicators", {"dataset": "IIP", "base_year": "2011-12", "frequency": "Monthly", "user_query": "general index growth"}),
    ("get_indicators", {"dataset": "PLFS", "frequency_code": 2, "user_query": "unemployment rate quarterly urban"}),
    ("get_indicators", {"dataset": "PLFS", "frequency_code": 1, "user_query": "unemployment rate annual"}),
    ("get_metadata", {"dataset": "NAS", "indicator_code": "22", "base_year": "2022-23", "frequency": "Quarterly"}),
]
OUT = Path("scripts/mospi_probe2_output.txt")


async def main() -> None:
    client = MultiServerMCPClient({"mospi": {"transport": "streamable_http", "url": get_settings().mospi_mcp_url}})
    parts = []
    async with client.session("mospi") as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        for tool, args in CALLS:
            for attempt in (1, 2):
                try:
                    text = tool_text(await tools[tool].ainvoke(args))
                except Exception as exc:
                    text = f"failed: {type(exc).__name__}: {exc}"
                if "error" not in text[:40] and "failed" not in text[:10]:
                    break
                await asyncio.sleep(5)
            parts.append(f"=== {tool} {args} (attempt {attempt})\n{text}")
            print(f"\n=== {tool} {args} (attempt {attempt})\n{text[:1500]}")
    OUT.write_text("\n\n".join(parts), encoding="utf-8")
    print(f"\n(full answers saved to {OUT})")


if __name__ == "__main__":
    asyncio.run(main())
