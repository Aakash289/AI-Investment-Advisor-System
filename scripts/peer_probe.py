"""
scripts/peer_probe.py: one-off check of free ways to find a company's peers (build step 7b).

What this file does:
    The valuation step compares a company's P/E, P/B and EV/EBITDA with the median of
    similar companies (design 14.9). That needs two things: a list of peers, and their
    prices and figures. Before writing the code we look at what each free option really
    gives, for Apple (US) and Reliance (India):

      A. yfinance       the company's Yahoo industry, and Yahoo's list of top companies
                        in that industry (US and India)
      B. yfinance       one batched price download for several tickers at once
      C. FMP free plan  the stock-peers endpoint (1 call of the 250 a day)
      D. SEC EDGAR      companies with the same SIC industry code, and a "frame": one
                        figure for every US filer in one call
      E. Trendlyne      whether it has industry or sector P/E and P/B parameters, and
                        what search_entities returns for an industry name (about 5 calls
                        of the 400 a day)

    It prints a short summary of each. No keys or web addresses are printed.

Run with:
    uv run python scripts/peer_probe.py
"""

import asyncio
import re

# Importing the package first switches on the rule that keeps secrets out of logs.
import invest_agent  # noqa: F401
import httpx

from invest_agent.config import get_settings


def section(title: str) -> None:
    print(f"\n--- {title}")


def short(text: str, n: int = 600) -> str:
    text = str(text)
    return text[:n] + (" ..." if len(text) > n else "")


def yahoo_industry(symbol: str) -> None:
    import yfinance as yf
    info = yf.Ticker(symbol).info
    keys = {k: info.get(k) for k in ("sector", "sectorKey", "industry", "industryKey", "marketCap", "currency")}
    print(symbol, keys)
    if not info.get("industryKey"):
        return
    try:
        industry = yf.Industry(info["industryKey"])
        top = industry.top_companies
        print(f"top companies in {info['industryKey']}: {0 if top is None else len(top)} rows")
        if top is not None:
            print(top.head(12).to_string())
    except Exception as exc:
        print("yf.Industry failed:", type(exc).__name__, short(exc, 200))


def main_sync() -> None:
    section("A. yfinance industry and top companies")
    for symbol in ("AAPL", "RELIANCE.NS"):
        try:
            yahoo_industry(symbol)
        except Exception as exc:
            print(symbol, "failed:", type(exc).__name__, short(exc, 200))

    section("B. yfinance batched prices (5 tickers in one call)")
    try:
        import yfinance as yf
        table = yf.download(["MSFT", "GOOGL", "DELL", "HPQ", "SONY"], period="5d", progress=False, auto_adjust=False)
        print(table["Close"].tail(2).round(2).to_string())
    except Exception as exc:
        print("failed:", type(exc).__name__, short(exc, 200))

    section("C. FMP free plan: stock peers for AAPL")
    key = get_settings().financial_modeling_prep_api_key
    if not key:
        print("skipped: no FMP key")
    else:
        r = httpx.get("https://financialmodelingprep.com/stable/stock-peers", params={"symbol": "AAPL", "apikey": key}, timeout=15)
        print("HTTP", r.status_code)
        print(short(r.text, 500))

    section("D. SEC EDGAR: companies with SIC 3571, and one frame")
    headers = {"User-Agent": get_settings().sec_edgar_user_agent}
    r = httpx.get("https://www.sec.gov/cgi-bin/browse-edgar",
                  params={"action": "getcompany", "SIC": "3571", "type": "10-K", "count": "100", "output": "atom"},
                  headers=headers, timeout=20)
    names = re.findall(r"<title>([^<]+)</title>", r.text)
    print("browse by SIC: HTTP", r.status_code, "|", len(names), "titles; first:", names[:8])
    r = httpx.get("https://data.sec.gov/api/xbrl/frames/us-gaap/NetIncomeLoss/USD/CY2024.json", headers=headers, timeout=30)
    if r.status_code == 200:
        data = r.json().get("data", [])
        print("frame NetIncomeLoss CY2024: HTTP 200 |", len(data), "companies; first:", data[:2])
    else:
        print("frame: HTTP", r.status_code)


async def trendlyne_part() -> None:
    section("E. Trendlyne: industry valuation parameters and industry search")
    if not get_settings().trendlyne_mcp_url:
        print("skipped: TRENDLYNE_MCP_URL not set")
        return
    from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne
    for query in ("industry PE", "sector PE", "industry PBV"):
        try:
            text = await call_trendlyne("search_financial_parameters", {"query": query})
            print(f"[search_financial_parameters '{query}']", short(text, 700))
        except Exception as exc:
            print(query, "failed:", type(exc).__name__, short(exc, 300))
    try:
        text = await call_trendlyne("search_entities", {"query": "Refineries", "limit": 10})
        print("[search_entities 'Refineries']", short(text, 900))
    except Exception as exc:
        print("search_entities failed:", type(exc).__name__, short(exc, 300))


if __name__ == "__main__":
    main_sync()
    asyncio.run(trendlyne_part())
