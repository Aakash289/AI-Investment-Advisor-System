"""
scripts/trendlyne_check_values.py: Trendlyne first-month checks 2 and 3, with real values.

What this file does:
    Fetches Reliance Industries' figures for six annual years for every code that
    exists (the first run showed which ones do not), and searches for a free cash flow
    or capex history, the one gap found. Saves the raw answers to
    data/trendlyne_values.txt, showing the units, fiscal years, and whether figures
    are consolidated or standalone.

    Uses 5 of the plan's 400 daily tool calls.

Run with:
    uv run python scripts/trendlyne_check_values.py
"""

import asyncio
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from invest_agent.config import get_settings


def six_years(code: str) -> list[str]:
    """The latest annual code plus its 1 to 5 years ago versions: sra, sramy1 ... sramy5."""
    return [code] + [f"{code}my{n}" for n in range(1, 6)]


# Only codes Trendlyne accepted in the first run.
CALL_1 = (six_years("sra") + six_years("npa") + six_years("opa") + six_years("inta")
          + six_years("depa") + six_years("cfoa") + ["ebidta", "ebidtamy1", "taxa", "taxamy1"])
CALL_2 = (six_years("totalshareholdersfundsa")
          + ["capitalexpenditurea", "borrowingsa", "netdebta", "cashandcashequivalentendofyeara",
             "shareissueda", "dividendpersharea", "longtermborrowingsa", "shorttermborrowingsa",
             "totaloperatingrevenuesa", "debtcea", "rev5a", "epsa", "basicepsa",
             "vfhymy4epsa", "vfhymy5epsa"])
# The one gap: a way to get free cash flow, or capex, for past years.
SEARCHES = ["free cash flow annual", "capital expenditure annual 1 year ago", "cash from investing activity annual"]


async def main() -> None:
    url = get_settings().trendlyne_mcp_url
    client = MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": url}})
    tools = {t.name: t for t in await client.get_tools()}

    lines = []
    for i, params in enumerate([CALL_1, CALL_2], start=1):
        result = await tools["get_stock_parameter_values"].ainvoke({"stock_codes": ["RELIANCE"], "parameters": params})
        lines += [f"=== values call {i}: {len(params)} codes", str(result), ""]
        print(f"values call {i} done")

    for q in SEARCHES:
        result = await tools["search_financial_parameters"].ainvoke({"query": q})
        lines += [f"=== search_financial_parameters: {q}", str(result), ""]
        print(f"search done: {q}")

    out = Path("data/trendlyne_values.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved to {out} (5 tool calls used)")


if __name__ == "__main__":
    asyncio.run(main())