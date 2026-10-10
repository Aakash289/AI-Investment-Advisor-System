"""
scripts/peer_probe_india.py: a second, small Trendlyne check for Indian peer valuation (build step 7b).

What this file does:
    The first peer probe showed that Trendlyne can list a company's industry peers
    (search_entities) and has P/E and P/B parameters (pettm, pbva), but not what an
    answer looks like when several stocks are asked for in one call, or which codes
    hold market value and EV/EBITDA. This asks, in 4 Trendlyne calls:

      1. search_financial_parameters "EV EBITDA"
      2. search_financial_parameters "market capitalization"
      3. search_entities for the industry with a larger limit, to see how many peers come back
      4. get_stock_parameter_values for Reliance and two peers at once, with pettm and pbva

    It saves the answer to call 4 in tests/fixtures/trendlyne/peers_values.json for the
    unit tests, and prints everything. No web addresses are printed.

Run with:
    uv run python scripts/peer_probe_india.py
"""

import asyncio
from pathlib import Path

# Importing the package first switches on the rule that keeps secrets out of logs.
import invest_agent  # noqa: F401

from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne


def short(text: str, n: int = 1500) -> str:
    return text[:n] + (" ..." if len(text) > n else "")


async def main() -> None:
    for query in ("EV EBITDA", "market capitalization"):
        print(f"\n--- search_financial_parameters '{query}'")
        print(short(await call_trendlyne("search_financial_parameters", {"query": query}), 1200))

    print("\n--- search_entities 'Refineries/Petro-Products', limit 25")
    print(short(await call_trendlyne("search_entities", {"query": "Refineries/Petro-Products", "limit": 25}), 2500))

    print("\n--- get_stock_parameter_values for RELIANCE, BPCL, HINDPETRO with pettm and pbva")
    text = await call_trendlyne("get_stock_parameter_values",
                                {"stock_codes": ["RELIANCE", "BPCL", "HINDPETRO"], "parameters": ["pettm", "pbva"]})
    print(short(text, 2500))
    path = Path("tests/fixtures/trendlyne/peers_values.json")
    path.write_text(text, encoding="utf-8")
    print(f"(saved to {path})")


if __name__ == "__main__":
    asyncio.run(main())
