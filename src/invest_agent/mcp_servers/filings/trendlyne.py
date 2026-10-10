"""
mcp_servers/filings/trendlyne.py: Indian fundamentals from Trendlyne's MCP server, and the shared Trendlyne client.

What this file does:
    NSE and BSE offer no free official API like SEC's, so Indian fundamentals come from
    Trendlyne MCP Pro (design 14.3, decided 2 October, checks passed 5 October). For one
    company this makes three Trendlyne calls:

      1. search_entities          the company's sector and industry (used for bank mode
                                  and, later, Indian peer groups)
      2. get_stock_parameter_values, six years of the history codes (about 43 codes)
      3. get_stock_parameter_values, the latest-year-only codes (about 9 codes)

    Trendlyne answers in text, one block per figure, labelled in words
    ("Rev. Ann. 2Y ago" then "RELIANCE:917121.00"), so parse_parameter_values() reads
    the label, works out the figure and how many years ago it is, and converts Rs crore
    to rupees. The codes and labels live in trendlyne_field_map.yaml.

    Our own filings server is itself a client of Trendlyne's server here: MCP servers
    can call other MCP servers in exactly the same way the app calls ours.

    Every call goes through tracked_call, so it counts against the plan's 400 calls a
    day and 2,000 a month.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from invest_agent.common.yaml_loader import load_yaml
from invest_agent.config import get_settings
from invest_agent.observability.tracked_call import tracked_call

logger = logging.getLogger(__name__)

MAX_CODES_PER_CALL = 50
_YEARS_AGO = re.compile(r"\b(\d)\s*y(?:r)?\s*ago\b")


class TrendlyneNotConfigured(RuntimeError):
    """TRENDLYNE_MCP_URL is missing from .env."""


# --- Parsing Trendlyne's answers (pure functions, tested with the real Reliance answer) --

def tool_text(result: Any) -> str:
    """Tool answers arrive as a list of content blocks; join their text."""
    if isinstance(result, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    return str(result)


def latest_fiscal_year(today: date | None = None) -> int:
    """Indian companies' year ends on 31 March and annual results are out by about the
    end of May, so from June the latest year is the one that ended this March."""
    today = today or date.today()
    return today.year if today.month >= 6 else today.year - 1


def _clean_label(label: str) -> tuple[str, int]:
    """("Rev. Ann. 2Y ago") -> ("rev ann", 2); ("Total Rev. Ann.") -> ("total rev ann", 0)."""
    text = re.sub(r"[^a-z0-9 ]+", " ", label.lower())
    years_ago = 0
    if m := _YEARS_AGO.search(text):
        years_ago = int(m.group(1))
        text = _YEARS_AGO.sub(" ", text)
    return " ".join(text.split()), years_ago


def parse_parameter_values(text: str, stock_code: str, today: date | None = None) -> tuple[dict[str, list[dict]], list[str]]:
    """Turn a get_stock_parameter_values answer into {figure: [year rows, newest first]}.

    Returns (figures, flags). Money figures are converted from Rs crore to rupees.
    Values of None (not reported) are skipped, so a missing figure stays missing.
    """
    fmap = load_yaml("mcp_servers/filings/trendlyne_field_map.yaml")
    labels, not_money, crore = fmap["labels"], set(fmap["not_money"]), fmap["crore"]
    payload = json.loads(text)
    if payload.get("status") != "success":
        raise RuntimeError(f"Trendlyne error: {payload.get('message')}")

    fy0 = latest_fiscal_year(today)
    figures: dict[str, list[dict]] = {}
    flags: list[str] = []
    # Blocks are separated by "---"; each is "Label\nCODE:value".
    for block in str(payload["data"]).split("---"):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        value_line = next((ln for ln in lines if ln.upper().startswith(f"{stock_code.upper()}:")), None)
        if value_line is None or len(lines) < 2:
            continue
        label = lines[lines.index(value_line) - 1]
        raw = value_line.split(":", 1)[1].strip()
        if raw in ("", "None", "null"):
            continue
        clean, years_ago = _clean_label(label)
        figure = labels.get(clean)
        if figure is None:
            flags.append(f"unmapped_label:{label}")
            continue
        value = float(raw) * (1 if figure in not_money else crore)
        fiscal_year = fy0 - years_ago
        figures.setdefault(figure, []).append({
            "fiscal_year": fiscal_year, "period_end": f"{fiscal_year}-03-31", "value": value,
            "unit": "INR" if figure not in not_money else "number", "tag": f"trendlyne:{label}",
            "accession": None, "filed": None,
        })
    for rows in figures.values():
        rows.sort(key=lambda r: r["fiscal_year"], reverse=True)
    return figures, flags


def parse_entity_search(text: str, stock_code: str) -> dict:
    """Pick the row for this NSE code from search_entities' table: name, sector, industry."""
    lines = text.replace("\\n", "\n").splitlines()
    header = next((ln for ln in lines if "nse_code" in ln and "|" in ln), None)
    if header is None:
        return {}
    cols = [c.strip() for c in header.split("|")]
    for ln in lines[lines.index(header) + 1:]:
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) == len(cols):
            row = dict(zip(cols, cells))
            if row.get("nse_code", "").upper() == stock_code.upper():
                return row
    return {}


# --- Calling Trendlyne --------------------------------------------------------------------

def _client() -> MultiServerMCPClient:
    url = get_settings().trendlyne_mcp_url
    if not url:
        raise TrendlyneNotConfigured("TRENDLYNE_MCP_URL is not set in .env")
    return MultiServerMCPClient({"trendlyne": {"transport": "streamable_http", "url": url}})


async def call_trendlyne(tool: str, arguments: dict) -> str:
    """One Trendlyne tool call, counted as 1 unit of the plan's quota."""
    async def _call() -> str:
        async with _client().session("trendlyne") as session:
            tools = {t.name: t for t in await load_mcp_tools(session)}
            return tool_text(await tools[tool].ainvoke(arguments))
    return await tracked_call("trendlyne", tool, _call, quota_units=1, timeout=30)


def _unknown_codes(text: str) -> list[str]:
    """Codes listed in Trendlyne's "Could not resolve parameter token(s): ..." error."""
    try:
        message = json.loads(text).get("message", "")
    except json.JSONDecodeError:
        return []
    if "Could not resolve" not in message:
        return []
    return re.findall(r"[a-z0-9]+", message.split(":", 1)[-1].split("[code")[0])


async def fetch_values(stock_code: str, codes: list[str]) -> str:
    """get_stock_parameter_values for one stock. If Trendlyne rejects some codes (one
    unknown code fails the whole call), drop exactly those and ask once more."""
    text = await call_trendlyne("get_stock_parameter_values", {"stock_codes": [stock_code], "parameters": codes})
    unknown = _unknown_codes(text)
    if unknown:
        logger.warning("Trendlyne does not know %s; asking again without them", unknown)
        codes = [c for c in codes if c not in unknown]
        text = await call_trendlyne("get_stock_parameter_values", {"stock_codes": [stock_code], "parameters": codes})
    return text


async def fetch_indian_fundamentals(stock_code: str, company_name: str | None = None) -> dict:
    """Everything Trendlyne gives for one company: {figures, sector, industry, name, flags}.

    The company's row (sector and industry) is found by searching for its NSE code. Some
    codes are not words Trendlyne's search matches (NESTLEIND, found 10 October: no row, so
    no industry and no peers at all), so when the code finds nothing the company's name is
    searched as well, without "Limited" or "Ltd", and the row is still picked by its code."""
    fmap = load_yaml("mcp_servers/filings/trendlyne_field_map.yaml")
    history_codes = [c + suffix for c in fmap["history"] for suffix in ["", "my1", "my2", "my3", "my4", "my5"]]
    latest_codes = list(fmap["latest"])

    entity = parse_entity_search(
        await call_trendlyne("search_entities", {"query": stock_code, "limit": 5}), stock_code)
    entity_flags: list[str] = []
    if not entity and company_name:
        name = re.sub(r"\b(limited|ltd\.?)\s*$", "", company_name.strip(), flags=re.IGNORECASE).strip(" .,")
        entity = parse_entity_search(
            await call_trendlyne("search_entities", {"query": name, "limit": 10}), stock_code)
        entity_flags.append("entity_found_by_name" if entity else "entity_not_found")

    figures: dict[str, list[dict]] = {}
    flags: list[str] = []
    for codes in (history_codes, latest_codes):
        for i in range(0, len(codes), MAX_CODES_PER_CALL):
            part, part_flags = parse_parameter_values(await fetch_values(stock_code, codes[i:i + MAX_CODES_PER_CALL]), stock_code)
            for figure, rows in part.items():
                figures.setdefault(figure, []).extend(rows)
            flags += part_flags
    return {"figures": figures, "sector": entity.get("sector"), "industry": entity.get("industry"),
            "name": entity.get("name"), "flags": flags + entity_flags + ["fiscal_year_inferred"]}
