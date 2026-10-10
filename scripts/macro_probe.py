"""
scripts/macro_probe.py: one-off check of the free macro sources before step 8a is built.

What this file does:
    The Macro agent (design 14.5) describes the economic weather: interest rates,
    inflation, growth, jobs, the currency and credit. Before writing it we look at what
    each free source really sends back:

      A. FRED (US)          the latest values of the ten US series in the design
      B. MoSPI MCP (India)  the official Indian statistics server: can our code connect,
                            and which tools does it offer (names, descriptions, arguments)
      C. BIS                central bank policy rates (WS_CBPOL): is India included,
                            and which address format answers
      D. World Bank         annual GDP growth and inflation for China (the rest of the panel)
      E. IMF DataMapper     growth forecasts for the six panel economies

    No keys or web addresses are printed. FRED's key is passed as a parameter.

Run with:
    uv run python scripts/macro_probe.py
"""

import asyncio

# Importing the package first switches on the rule that keeps secrets out of logs.
import invest_agent  # noqa: F401
import httpx

from invest_agent.config import get_settings

FRED_SERIES = ["FEDFUNDS", "DGS10", "T10Y2Y", "CPIAUCSL", "GDPC1", "INDPRO", "UNRATE",
               "DTWEXBGS", "DEXINUS", "BAMLH0A0HYM2"]


def short(text, n=600):
    text = str(text)
    return text[:n] + (" ..." if len(text) > n else "")


def fred() -> None:
    print("\n--- A. FRED: latest two values of each series")
    key = get_settings().fred_api_key
    if not key:
        print("skipped: FRED_API_KEY not set")
        return
    with httpx.Client(timeout=15) as client:
        for series in FRED_SERIES:
            r = client.get("https://api.stlouisfed.org/fred/series/observations",
                           params={"series_id": series, "api_key": key, "file_type": "json",
                                   "sort_order": "desc", "limit": 2})
            obs = r.json().get("observations", []) if r.status_code == 200 else []
            print(f"{series:14} HTTP {r.status_code}  " + "  ".join(f"{o['date']}={o['value']}" for o in obs))


async def mospi() -> None:
    print("\n--- B. MoSPI MCP server: tools")
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools
    url = get_settings().mospi_mcp_url
    try:
        client = MultiServerMCPClient({"mospi": {"transport": "streamable_http", "url": url}})
        async with client.session("mospi") as session:
            tools = await load_mcp_tools(session)
            print(f"{len(tools)} tools")
            for t in tools:
                schema = t.args_schema if isinstance(t.args_schema, dict) else getattr(t.args_schema, "model_json_schema", lambda: {})()
                args = list((schema or {}).get("properties", {}).keys())
                print(f"- {t.name}: {short(t.description, 300)}\n    arguments: {args}")
    except Exception as exc:
        print("failed:", type(exc).__name__, short(exc, 400))


def bis() -> None:
    print("\n--- C. BIS policy rates, India and US")
    attempts = [
        ("v1 csv", "https://stats.bis.org/api/v1/data/WS_CBPOL/D.IN+US/all", {"lastNObservations": 2, "format": "csv"}),
        ("v1 monthly csv", "https://stats.bis.org/api/v1/data/WS_CBPOL/M.IN+US/all", {"lastNObservations": 2, "format": "csv"}),
        ("v2 csv", "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.IN+US",
         {"lastNObservations": 2, "format": "csv"}),
    ]
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for label, url, params in attempts:
            try:
                r = client.get(url, params=params)
                print(f"[{label}] HTTP {r.status_code}  {r.headers.get('content-type', '')}")
                print(short(r.text, 700))
            except Exception as exc:
                print(f"[{label}] failed:", type(exc).__name__, short(exc, 200))


def world_bank() -> None:
    print("\n--- D. World Bank: China GDP growth and inflation, latest 2 years")
    with httpx.Client(timeout=20) as client:
        for indicator in ("NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG"):
            r = client.get(f"https://api.worldbank.org/v2/country/CHN/indicator/{indicator}",
                           params={"format": "json", "mrv": 2})
            print(indicator, "HTTP", r.status_code, short(r.text, 400))


def imf() -> None:
    print("\n--- E. IMF DataMapper: real GDP growth forecasts")
    r = httpx.get("https://www.imf.org/external/datamapper/api/v1/NGDP_RPCH/USA/IND/CHN/JPN/GBR/EURO",
                  timeout=20, follow_redirects=True)
    print("HTTP", r.status_code)
    try:
        values = r.json().get("values", {}).get("NGDP_RPCH", {})
        for country, years in values.items():
            print(country, {y: years[y] for y in sorted(years)[-4:] if y in years})
    except Exception as exc:
        print("not JSON:", short(r.text, 300), type(exc).__name__)


if __name__ == "__main__":
    fred()
    asyncio.run(mospi())
    bis()
    world_bank()
    imf()
