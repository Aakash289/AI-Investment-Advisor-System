"""
mcp_servers/filings/trendlyne_segments.py: Indian revenue within and outside India (design 14.7).

What this file does:
    Trendlyne has no parameter for revenue by geography (the probe on 6 October searched
    for one), but its document search reads annual reports, and Indian companies report
    a "Secondary Segment Information" table there:

        | Segment Revenue External Turnover | 2025-26 | 2024-25 |
        | Within India                      | 7,48,203 | 6,86,534 |
        | Outside India                     | 4,27,716 | 3,84,640 |
        | Total                             | 11,75,919 | 10,71,174 |
    (Reliance, ₹ crore, FY2026 annual report)

    One document search (1 Trendlyne call) finds it; this file reads the first column
    (the latest year). Answers about other companies, which the search sometimes mixes
    in, are ignored: only the part of the answer under this company's header is read.
    The parts are checked against the table's own Total line, since this turnover is
    counted differently from the revenue in the Fundamentals agent.
"""

from __future__ import annotations

import re

from invest_agent.mcp_servers.filings.trendlyne import call_trendlyne

ROW = re.compile(r"\|\s*(Within India|Outside India|Total)\s*\|\s*([\d,\.]+)\s*\|")


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def company_part(text: str, ticker: str) -> str:
    """The stretch of the answer under this company's document header lines."""
    text = text.replace("\\n", "\n").replace("\\\\", "\\")
    pieces = re.split(r'(?="?\d+\|[^|\n]+\|[A-Z0-9&\-]+\|\d+\|[^|\n]+\|\d{4}-\d{2}-\d{2})', text)
    return "\n".join(p for p in pieces if re.match(rf'"?\d+\|[^|\n]+\|{re.escape(ticker)}\|', p))


def parse_within_outside(text: str, ticker: str) -> dict | None:
    """{"parts": [{name, value}], "reported_total", "unit": "INR crore"} or None."""
    found: dict[str, float] = {}
    for name, value in ROW.findall(company_part(text, ticker)):
        found.setdefault(name, _number(value))           # the first match is the latest year
    if "Within India" not in found or "Outside India" not in found:
        return None
    return {"parts": [{"name": "Within India", "value": found["Within India"]},
                      {"name": "Outside India", "value": found["Outside India"]}],
            "reported_total": found.get("Total"), "axis": "AnnualReportSecondarySegments", "unit": "INR crore"}


async def india_breakdown(ticker: str, company_name: str, call=call_trendlyne) -> dict | None:
    query = f"{company_name} annual report segment revenue within India and outside India"
    return parse_within_outside(await call("get_document_search_results", {"query": query}), ticker)
