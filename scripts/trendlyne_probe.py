"""
scripts/trendlyne_probe.py: writes down what each Trendlyne MCP tool does and expects.

What this file does:
    Connects to your Trendlyne MCP server (the URL in .env) and saves every tool's
    description and input fields to data/trendlyne_tools.txt, so the Trendlyne client
    in build step 5 can be written against the real tools. Listing tools does not use
    any of the plan's 400 daily tool calls. The URL itself is never printed or saved.

Run with:
    uv run python scripts/trendlyne_probe.py
"""

import asyncio
import json
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from invest_agent.config import get_settings


async def main() -> None:
    url = get_settings().trendlyne_mcp_url
    if not url:
        raise SystemExit("TRENDLYNE_MCP_URL is not set in .env")

    # streamable_http is the transport Trendlyne's server answered on in the first check.
    client = MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": url}})
    tools = await client.get_tools()

    lines = []
    for tool in tools:
        if tool.name.startswith("watchlist"):
            continue                      # not used by the agent
        schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.args_schema.model_json_schema()
        lines += [f"=== {tool.name}", (tool.description or "").strip(), json.dumps(schema, indent=2), ""]

    out = Path("data/trendlyne_tools.txt")
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {len(tools)} tool descriptions to {out}")


if __name__ == "__main__":
    asyncio.run(main())