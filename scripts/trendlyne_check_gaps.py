"""
scripts/trendlyne_check_gaps.py: the last Trendlyne field check before building step 5.

What this file does:
    Tries to fill the three gaps found for Reliance Industries: capex history (through
    fixed assets and capital work in progress, from which capex can be rebuilt), shares
    outstanding, and dividends. If Trendlyne rejects some codes, the script removes
    exactly those and asks again, so no calls are wasted on guesses. Saves everything
    to data/trendlyne_gaps.txt.

    Uses about 4 of the plan's 400 daily tool calls.

Run with:
    uv run python scripts/trendlyne_check_gaps.py
"""

import asyncio
import json
import re
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from invest_agent.config import get_settings


def six_years(code: str) -> list[str]:
    return [code] + [f"{code}my{n}" for n in range(1, 6)]


CODES = (six_years("fixedassetsa") + six_years("capitalworkinprogressa")
         + six_years("totaloperatingrevenuesa")
         + ["dividend1yactual", "dividend5yactual", "divlastyear", "cfia", "ncfa"])
SEARCHES = ["number of shares outstanding", "market capitalization"]


def text_of(result) -> str:
    """Pull the text out of the tool's answer (a list of content blocks)."""
    if isinstance(result, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    return str(result)


async def main() -> None:
    url = get_settings().trendlyne_mcp_url
    client = MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": url}})
    tools = {t.name: t for t in await client.get_tools()}
    lines, calls, codes = [], 0, list(CODES)

    # Ask for the values; if some codes are unknown, drop just those and ask again.
    for _ in range(3):
        result = text_of(await tools["get_stock_parameter_values"].ainvoke(
            {"stock_codes": ["RELIANCE"], "parameters": codes}))
        calls += 1
        answer = json.loads(result)
        if answer.get("status") == "success":
            lines += [f"=== values ({len(codes)} codes)", answer["data"], ""]
            break
        unknown = re.findall(r"[a-z0-9]+", answer.get("message", "").split(":", 1)[-1].split("[code")[0])
        lines += [f"=== not available: {', '.join(unknown)}", ""]
        codes = [c for c in codes if c not in unknown]
        print(f"dropped {len(unknown)} unknown codes, asking again")

    for q in SEARCHES:
        result = text_of(await tools["search_financial_parameters"].ainvoke({"query": q}))
        calls += 1
        lines += [f"=== search_financial_parameters: {q}", result, ""]

    out = Path("data/trendlyne_gaps.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved to {out} ({calls} tool calls used)")


if __name__ == "__main__":
    asyncio.run(main())