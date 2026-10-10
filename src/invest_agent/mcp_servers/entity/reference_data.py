"""
mcp_servers/entity/reference_data.py: the lists of every listed company we can match against.

What this file does:
    To turn "Apple" or "Infosys" into a ticker, the matcher needs a list of every
    listed company with its ticker, exchange and currency. This file builds that list
    from two free official sources:

      - SEC's company_tickers_exchange.json: every US-listed company with its SEC
        company ID (CIK), name, ticker and exchange.
      - NSE's EQUITY_L.csv: every company listed on India's National Stock Exchange,
        with its symbol, name and ISIN.

    Refresh rule (decided 4 October): on first use each day. A downloaded list is
    saved under data/reference/; if the saved copy is less than 24 hours old it is
    reused, otherwise it is downloaded again. If a download fails, the old saved copy
    is used and flagged as stale, so one bad download never stops the app.

    It also fetches a company's SEC profile (industry code and the annual report forms
    it files), which the coverage rules need: 20-F and 40-F filers and REITs are not
    covered in version 1.

Design reference: 14.1. Open item: no fallback yet if NSE blocks the download and no
saved copy exists; Indian companies then cannot be matched and the result says so.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# httpx: the HTTP client used for every web request in the app (supports async).
import httpx

from invest_agent.config import get_settings
from invest_agent.observability.tracked_call import tracked_call

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

MAX_AGE_SECONDS = 24 * 60 * 60

# NSE's archive server rejects requests that do not look like they come from a
# browser, so the NSE download sends an ordinary browser User-Agent.
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}


# A dataclass is a class whose main job is to hold data. @dataclass writes the
# __init__ and comparison methods for us; frozen=True makes instances read-only, so a
# Listing can safely be shared and used as a dict key.
@dataclass(frozen=True)
class Listing:
    """One tradable listing of one company."""

    ticker: str          # "AAPL", or the NSE symbol "INFY"
    name: str            # as the exchange or SEC writes it, e.g. "Apple Inc."
    exchange: str        # "Nasdaq", "NYSE", "NSE", ...
    market: str          # "US" or "IN"
    currency: str        # "USD" or "INR"
    cik: int | None = None     # SEC company ID, US only
    isin: str | None = None    # International ID, India only

    @property
    def yahoo_symbol(self) -> str:
        """The symbol yfinance expects: NSE tickers need the ".NS" suffix."""
        return f"{self.ticker}.NS" if self.market == "IN" else self.ticker

    @property
    def company_key(self) -> str:
        """Identifies the company rather than the listing, so two share classes of one
        company (GOOGL and GOOG) share a key."""
        return f"US:{self.cik}" if self.cik is not None else f"{self.market}:{self.isin or self.ticker}"

    def to_dict(self) -> dict:
        return asdict(self) | {"yahoo_symbol": self.yahoo_symbol}


@dataclass
class ReferenceData:
    """Everything loaded, plus notes about stale or missing lists."""

    listings: list[Listing] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


# --- Parsing (pure functions, tested offline) ---------------------------------------

def parse_sec_tickers(payload: dict) -> list[Listing]:
    """Turn SEC's JSON ({"fields": [...], "data": [[cik, name, ticker, exchange], ...]})
    into Listing objects. Rows with no exchange are skipped (no longer trading)."""
    fields = payload["fields"]
    idx = {name: fields.index(name) for name in ("cik", "name", "ticker", "exchange")}
    listings = []
    for row in payload["data"]:
        exchange = row[idx["exchange"]]
        ticker = row[idx["ticker"]]
        if not exchange or not ticker:
            continue
        listings.append(Listing(
            ticker=str(ticker).upper(),
            name=str(row[idx["name"]]),
            exchange=str(exchange),
            market="US",
            currency="USD",
            cik=int(row[idx["cik"]]),
        ))
    return listings


def parse_nse_equity_csv(text: str) -> list[Listing]:
    """Turn NSE's EQUITY_L.csv into Listing objects.

    The file's header names carry stray spaces (" SERIES"), so they are stripped.
    csv.DictReader reads each row into a dict keyed by those header names.
    """
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [h.strip() for h in (reader.fieldnames or [])]
    listings = []
    for row in reader:
        symbol = (row.get("SYMBOL") or "").strip()
        name = (row.get("NAME OF COMPANY") or "").strip()
        if not symbol or not name:
            continue
        listings.append(Listing(
            ticker=symbol.upper(),
            name=name,
            exchange="NSE",
            market="IN",
            currency="INR",
            isin=(row.get("ISIN NUMBER") or "").strip() or None,
        ))
    return listings


# --- Download with the daily refresh rule --------------------------------------------

def _reference_dir() -> Path:
    """data/reference/, next to the SQLite file."""
    folder = Path(get_settings().database_path).parent / "reference"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _is_fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < MAX_AGE_SECONDS


async def _download_text(url: str, headers: dict) -> str:
    # AsyncClient is httpx's asynchronous client: while it waits on the network, other
    # work (other parallel requests) can run. "async with" closes it afterwards.
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()      # turns 4xx and 5xx answers into an exception
        return response.text


async def _load_one(name: str, url: str, headers: dict, service: str, flags: list[str]) -> str | None:
    """Return the list's text: the saved copy if fresh, else a new download, else the
    stale saved copy (flagged), else None (flagged)."""
    path = _reference_dir() / name
    if _is_fresh(path):
        return path.read_text(encoding="utf-8")
    try:
        text = await tracked_call(service, f"download_{name}", _download_text, url, headers, timeout=30)
        path.write_text(text, encoding="utf-8")
        return text
    except Exception as err:          # any failure falls back to the saved copy
        logger.warning("Could not download %s: %s", name, err)
        if path.exists():
            flags.append(f"{name}_stale")
            return path.read_text(encoding="utf-8")
        flags.append(f"{name}_unavailable")
        return None


async def load_reference_data() -> ReferenceData:
    """Load both lists, applying the refresh-on-first-use-each-day rule."""
    settings = get_settings()
    ref = ReferenceData()

    if not settings.sec_edgar_user_agent:
        ref.flags.append("sec_user_agent_missing")
    sec_text = await _load_one(
        "sec_company_tickers_exchange.json", SEC_TICKERS_URL,
        {"User-Agent": settings.sec_edgar_user_agent or "invest-agent"}, "sec_edgar", ref.flags,
    )
    if sec_text:
        ref.listings.extend(parse_sec_tickers(json.loads(sec_text)))

    nse_text = await _load_one("nse_equity_l.csv", NSE_EQUITY_URL, NSE_HEADERS, "nse", ref.flags)
    if nse_text:
        ref.listings.extend(parse_nse_equity_csv(nse_text))

    logger.info("Reference data: %d listings, flags=%s", len(ref.listings), ref.flags)
    return ref


# --- SEC company profile, for the coverage rules -----------------------------------------

async def fetch_sec_profile(cik: int) -> dict:
    """Return {"sic": 3571, "sic_description": "...", "annual_forms": ["10-K", ...]}.

    SEC's submissions file lists every recent filing's form type; the annual report
    form tells us whether the company is a domestic filer (10-K) or a foreign one
    (20-F, 40-F), and the SIC code is its industry (6798 means REIT).
    """
    settings = get_settings()
    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    text = await tracked_call(
        "sec_edgar", "submissions", _download_text, url,
        {"User-Agent": settings.sec_edgar_user_agent or "invest-agent"},
    )
    data = json.loads(text)
    forms = data.get("filings", {}).get("recent", {}).get("form", [])
    annual = sorted({f for f in forms if f in {"10-K", "20-F", "40-F"}})
    sic = data.get("sic")
    return {
        "sic": int(sic) if sic and str(sic).isdigit() else None,
        "sic_description": data.get("sicDescription"),
        "annual_forms": annual,
    }