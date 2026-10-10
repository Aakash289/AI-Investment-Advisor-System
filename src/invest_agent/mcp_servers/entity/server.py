"""
mcp_servers/entity/server.py: entity_server, the MCP server behind Entity Resolution.

What this file does:
    Runs a small MCP server that offers one tool, resolve_ticker(query, confirmed).
    The Entity Resolution agent calls this tool (through langchain-mcp-adapters, see
    mcp_servers/clients.py) after the model has pulled the company reference out of
    the user's message. The tool:
      1. makes sure today's reference lists are loaded (reference_data.py);
      2. runs the matcher (matcher.py);
      3. for a resolved US company, fetches its SEC profile and applies the version 1
         coverage rules; for an Indian one, applies the name-based REIT/InvIT rule;
      4. returns a plain dict (status, listing, candidates, question, reason, flags).

What MCP is, briefly:
    The Model Context Protocol is a standard way for a program to offer "tools" that
    an AI application can discover and call. Each of our six data servers is a
    separate MCP server, so each data source is isolated, testable on its own, and
    callable from LangGraph the same way.

Run it by itself (useful for debugging) with:
    uv run python -m invest_agent.mcp_servers.entity.server
It then waits for an MCP client on standard input and output; Ctrl + C stops it.
"""

from __future__ import annotations

import logging
from datetime import date

# FastMCP is the high-level server class in the official MCP Python SDK. Decorating a
# function with @mcp.tool() publishes it as a tool: its name, its arguments (from the
# type hints) and its description (from the docstring) are sent to the client.
from mcp.server.fastmcp import FastMCP

from invest_agent.common.logging_setup import setup_logging
from invest_agent.mcp_servers.entity.matcher import check_coverage, resolve_with_guess
from invest_agent.mcp_servers.entity.reference_data import ReferenceData, fetch_sec_profile, load_reference_data

logger = logging.getLogger(__name__)

mcp = FastMCP("entity_server")

# The loaded lists, kept in memory between tool calls, and the day they were loaded,
# so a server left running overnight reloads on the first call of the next day.
_reference: ReferenceData | None = None
_loaded_on: date | None = None


async def _get_reference() -> ReferenceData:
    global _reference, _loaded_on
    if _reference is None or _loaded_on != date.today():
        _reference = await load_reference_data()
        _loaded_on = date.today()
    return _reference


async def resolve_ticker_logic(query: str, confirmed: bool = False, full_name_guess: str | None = None,
                               reference: ReferenceData | None = None) -> dict:
    """The tool's work, separated from the MCP wrapper so tests can call it directly."""
    ref = reference or await _get_reference()
    result = resolve_with_guess(query, full_name_guess, ref.listings, confirmed=confirmed)
    output = result.to_dict()
    output["flags"] = list(ref.flags)

    if result.status == "resolved" and result.listing:
        listing = result.listing
        profile = None
        if listing.market == "US" and listing.cik:
            try:
                profile = await fetch_sec_profile(listing.cik)
                output["sec_profile"] = profile
            except Exception as err:      # coverage cannot be checked; carry on, flagged
                logger.warning("SEC profile unavailable for %s: %s", listing.ticker, err)
                output["flags"].append("sec_profile_unavailable")
        reason = check_coverage(listing, profile)
        if reason:
            output["status"] = "not_covered"
            output["reason"] = reason
    return output


@mcp.tool()
async def resolve_ticker(query: str, confirmed: bool = False, full_name_guess: str | None = None) -> dict:
    """Resolve a company name or ticker to one listing.

    query: the company reference in the user's words, e.g. "apple", "Infosys", "RELIANCE.NS".
    confirmed: true when the user has already picked this exact listing after a
        question, so the cross-listing question is not asked again.
    full_name_guess: the model's guess at the full company name ("Reliance Industries").
        It is compared with the user's words; if they point to different companies,
        the user is asked, with the guess listed first.

    Returns status (resolved, ask_user, not_found, not_covered), the listing (ticker,
    exchange, market, currency, CIK or ISIN, Yahoo symbol), candidates and a question
    when the user must choose, the reason when not covered, and data flags.
    """
    return await resolve_ticker_logic(query, confirmed, full_name_guess)


if __name__ == "__main__":
    # This block runs only when the file is started directly (python -m ...), not when
    # it is imported. mcp.run() serves over stdio: requests arrive on standard input
    # and replies go to standard output, which is how the client process talks to it.
    # Logging therefore goes to standard error, so it never mixes with the replies.
    setup_logging()
    mcp.run()