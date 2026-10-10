"""
scripts/geo_probe.py: one-off check of geography and sector sources before step 8c is built.

What this file does:
    The Geographic and Sector agent (design 14.7) needs two things: how much of a
    company's revenue comes from each country or region, and which of 11 sectors it
    belongs to. Before writing it we look at what the free sources really give:

      A. edgartools (US)   Apple's latest 10-K, read in full, and the revenue figures
                           broken down by geography (StatementGeographicalAxis). SEC's
                           company facts file leaves these out, which is why the design
                           uses the full filing.
      B. yfinance          Yahoo's sector and industry for Apple and Reliance (the probe in
                           step 7b already saw them; repeated here next to the others)
      C. Trendlyne         whether it has parameters for revenue by geography or exports
                           (2 searches), and what its document search says about Reliance's
                           revenue from outside India (1 call): 3 calls of the 400 a day

    Needs the edgartools library first:  uv add edgartools
    No keys or web addresses are printed.

Run with:
    uv run python scripts/geo_probe.py
"""

import asyncio

import invest_agent  # noqa: F401

from invest_agent.config import get_settings


def short(text, n=1200):
    text = str(text)
    return text[:n] + (" ..." if len(text) > n else "")


def edgar_geography() -> None:
    print("--- A. edgartools: Apple's latest 10-K, revenue by geography")
    try:
        from edgar import Company, set_identity
    except ImportError:
        print("edgartools is not installed: run  uv add edgartools  and try again")
        return
    set_identity(get_settings().sec_edgar_user_agent)
    filing = Company("AAPL").get_filings(form="10-K").latest()
    print("filing:", filing.form, filing.filing_date, filing.accession_no)
    xbrl = filing.xbrl()
    df = xbrl.facts.to_dataframe()
    print("fact table:", df.shape, "columns:", list(df.columns)[:40])
    geo_cols = [c for c in df.columns if "geograph" in c.lower() or "axis" in c.lower()]
    print("dimension columns:", geo_cols)
    for col in geo_cols:
        rows = df[df[col].notna()]
        concept_col = next((c for c in ("concept", "element", "name") if c in df.columns), None)
        if concept_col:
            rows = rows[rows[concept_col].astype(str).str.contains("Revenue", case=False)]
        keep = [c for c in (concept_col, col, "value", "numeric_value", "period_end", "period_start") if c in df.columns]
        print(f"\nrevenue facts with {col}: {len(rows)}")
        print(rows[keep].head(25).to_string())


def yahoo_sector() -> None:
    print("\n--- B. yfinance sector and industry")
    import yfinance as yf
    for symbol in ("AAPL", "RELIANCE.NS"):
        info = yf.Ticker(symbol).info
        print(symbol, {k: info.get(k) for k in ("sector", "sectorKey", "industry", "industryKey", "country")})


async def trendlyne_geography() -> None:
    print("\n--- C. Trendlyne: geography and export revenue")
    if not get_settings().trendlyne_mcp_url:
        print("skipped: TRENDLYNE_MCP_URL not set")
        return
    from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne
    for query in ("revenue by geography segment", "export revenue"):
        print(f"[search_financial_parameters '{query}']", short(await call_trendlyne("search_financial_parameters",
                                                                                    {"query": query}), 900))
    print("[get_document_search_results]", short(await call_trendlyne(
        "get_document_search_results",
        {"query": "Reliance Industries FY2026 annual report segment revenue within India and outside India"}), 2500))


if __name__ == "__main__":
    for part in (edgar_geography, yahoo_sector):
        try:
            part()
        except Exception as exc:
            print(f"{part.__name__} failed:", type(exc).__name__, short(exc, 400))
    asyncio.run(trendlyne_geography())
