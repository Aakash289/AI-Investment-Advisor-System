"""
scripts/mospi_probe.py: find the MoSPI codes for India's inflation, GDP, industrial
production and unemployment (build step 8a).

What this file does:
    MoSPI's official MCP server works (macro probe, 5 October) but its filter codes are
    arbitrary and dataset-specific, so they cannot be guessed. Its tools are meant to be
    used in order: list_datasets, then get_indicators for a dataset, then get_metadata for
    an indicator, then get_data. This script runs the first two steps and prints the
    answers, so the codes can be read off and fixed in series_map.yaml:

      1. list_datasets
      2. get_indicators for the datasets the Macro agent needs: CPI (inflation),
         NAS (national accounts, GDP), IIP (industrial production), PLFS (unemployment)

    It also saves the full answers to scripts/mospi_probe_output.txt, since they are long.
    MoSPI needs no key and costs nothing.

Run with:
    uv run python scripts/mospi_probe.py
"""

import asyncio
from pathlib import Path

import invest_agent  # noqa: F401
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from invest_agent.config import get_settings
from invest_agent.mcp_servers.filings.trendlyne import tool_text

DATASETS = ["CPI", "NAS", "IIP", "PLFS"]
OUT = Path("scripts/mospi_probe_output.txt")


async def main() -> None:
    client = MultiServerMCPClient({"mospi": {"transport": "streamable_http", "url": get_settings().mospi_mcp_url}})
    parts = []
    async with client.session("mospi") as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}

        text = tool_text(await tools["list_datasets"].ainvoke({}))
        parts.append("=== list_datasets\n" + text)
        print("=== list_datasets (first 3000 characters)\n" + text[:3000])

        for name in DATASETS:
            try:
                text = tool_text(await tools["get_indicators"].ainvoke({"dataset": name, "user_query": "headline all India"}))
            except Exception as exc:
                text = f"failed: {type(exc).__name__}: {exc}"
            parts.append(f"=== get_indicators {name}\n" + text)
            print(f"\n=== get_indicators {name} (first 2500 characters)\n" + text[:2500])

    OUT.write_text("\n\n".join(parts), encoding="utf-8")
    print(f"\n(full answers saved to {OUT})")


if __name__ == "__main__":
    asyncio.run(main())
