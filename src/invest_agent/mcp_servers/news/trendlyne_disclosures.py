"""
mcp_servers/news/trendlyne_disclosures.py: Indian news, exchange announcements and insider trades from Trendlyne.

What this file does:
    India has no SEC-style feed of official filings, so Trendlyne's tools fill that role
    (design 14.6). Checked on 5 October with scripts/news_probe2.py and news_probe3.py:

      get_overview_news_corp_events, type "news"
          a table of the latest news and exchange announcements: date, title,
          description, link and outlet (2 calls a company is all news needs)
      get_ownership_deals_insider_sast, type "sast"
          insider trades and promoter dealings: who, their role, acquisition, disposal,
          pledge or revocation, the date, the number of shares and how it was done

    Both answers are text. The news table uses " | " between columns, and a description
    can itself contain " | ", so the columns are found by position from the left (date
    and title) and by shape (the link starts with http).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta

from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne

# The report's news section covers the last month, so insider trades older than that are
# left out (Star Health's 28 July promoter trades showed in an October report). Was 6.
INSIDER_MONTHS = 1
# A promoter disposal and a promoter acquisition off market on the same day, within this
# share of each other, is one transfer inside the promoter group (an estate passing shares
# to a family entity), not a sale and a purchase.
TRANSFER_TOLERANCE = 0.05


def parse_news(text: str, limit: int = 15) -> list[dict]:
    lines = [ln.strip() for ln in text.replace("\\n", "\n").splitlines()]
    header = next((ln for ln in lines if ln.startswith("NSEcode |") and "pubDate" in ln), None)
    if header is None:
        return []
    cols = [c.strip() for c in header.split(" | ")]
    i_date, i_title, i_desc = cols.index("pubDate"), cols.index("title"), cols.index("description")
    items = []
    for ln in lines[lines.index(header) + 1:]:
        cells = [c.strip() for c in ln.split(" | ")]
        if len(cells) <= i_desc:
            continue
        url = next((c for c in cells[i_desc:] if c.startswith("http")), None)
        source = cells[cells.index(url) + 1] if url and cells.index(url) + 1 < len(cells) else None
        items.append({"title": cells[i_title], "snippet": cells[i_desc][:500], "date": cells[i_date][:10],
                      "url": url, "outlet": source or "Trendlyne", "origin": "trendlyne_news", "trusted": True})
        if len(items) >= limit:
            break
    return items


def _lists(text: str) -> list[list]:
    """Every top-level [...] list after "tableData:", read as JSON."""
    start = text.find("tableData:")
    body, out, depth, begin = text[start:] if start >= 0 else "", [], 0, None
    for i, ch in enumerate(body):
        if ch == "[":
            depth += 1
            if depth == 1:
                begin = i
        elif ch == "]":
            depth -= 1
            if depth == 0 and begin is not None:
                try:
                    out.append(json.loads(body[begin:i + 1]))
                except json.JSONDecodeError:
                    pass
    return out


def _same_transfer(a: list, b: list) -> bool:
    """a is a promoter disposal and b a promoter acquisition, same day, off market, similar size."""
    qa, qb = a[4], b[4]
    return (str(a[2]).lower() == "disposal" and str(b[2]).lower() == "acquisition" and a[3] == b[3]
            and all(re.search(r"promoter", str(r[1] or ""), re.I) for r in (a, b))
            and all("off market" in str(r[14] or "").lower() for r in (a, b))
            and isinstance(qa, (int, float)) and isinstance(qb, (int, float)) and max(qa, qb) > 0
            and abs(qa - qb) / max(qa, qb) <= TRANSFER_TOLERANCE)


def parse_insider(text: str, today: date | None = None) -> list[dict]:
    """Insider and promoter trades from the last month, as news-like items. A matching
    promoter disposal and acquisition become one "transfer within the promoter group"."""
    today = today or date.today()
    cutoff = (today - timedelta(days=round(INSIDER_MONTHS * 30.44))).isoformat()
    rows = [r for r in _lists(text) if len(r) >= 15 and isinstance(r[3], str) and r[3] >= cutoff]
    paired: set[int] = set()
    items = []
    for i, a in enumerate(rows):
        for j, b in enumerate(rows):
            if i in paired or j in paired or i == j or not _same_transfer(a, b):
                continue
            paired |= {i, j}
            shares = f"{int(a[4]):,} shares"
            items.append({"title": f"Promoter shares moved within the promoter group: {a[0]} to {b[0]}, {shares}, "
                                   f"off market",
                          "snippet": f"Insider trading disclosures dated {a[3]}: {a[0]} disposed of {shares} and "
                                     f"{b[0]} acquired {int(b[4]):,} shares off market the same day. The promoter "
                                     f"group's total holding barely changes.",
                          "date": a[3], "url": None, "outlet": "Exchange disclosure (via Trendlyne)",
                          "origin": "trendlyne_insider", "trusted": True, "promoter": True,
                          "internal_transfer": True})
    for i, row in enumerate(rows):
        if i in paired:
            continue
        name, role, action, when, qty, mode = row[0], row[1], row[2], row[3], row[4], row[14]
        shares = f"{int(qty):,} shares" if isinstance(qty, (int, float)) else "shares"
        items.append({"title": f"{name} ({role}): {action.lower()} of {shares}, {mode}",
                      "snippet": f"Insider trading disclosure dated {when}: {name}, {role}, {action}, {shares}, {mode}.",
                      "date": when, "url": None, "outlet": "Exchange disclosure (via Trendlyne)",
                      "origin": "trendlyne_insider", "trusted": True,
                      "promoter": bool(re.search(r"promoter", role or "", re.I))})
    return items


async def disclosures(ticker: str, call=call_trendlyne) -> tuple[list[dict], list[str]]:
    """News and insider trades, both asked at the same time (speed, 7 October)."""
    news, insider = await asyncio.gather(
        call("get_overview_news_corp_events", {"stock_code": ticker, "type": "news"}),
        call("get_ownership_deals_insider_sast", {"stock_code": ticker, "type": "sast"}),
        return_exceptions=True)
    items, flags = [], []
    if isinstance(news, BaseException):
        flags.append("trendlyne_news_unavailable")
    else:
        items += parse_news(news)
    if isinstance(insider, BaseException):
        flags.append("trendlyne_insider_unavailable")
    else:
        items += parse_insider(insider)
    return items, flags
