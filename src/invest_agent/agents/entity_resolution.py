"""
agents/entity_resolution.py: the Entity Resolution node, the first step of every request.

What this file does:
    Takes the user's raw message ("should I invest in apple, I have 2 lakhs") and
    works out exactly which listed company it is about. It does this in two parts:

      1. One small call to the cheap model (GPT-5.6 Luna) pulls out just the company
         reference ("apple"), says whether it was written as a ticker, and, if the
         previous turn asked the user to choose between options, which one they chose.
      2. The entity_server's resolve_ticker tool (an MCP call) does the actual
         matching against SEC and NSE lists, plain code, no model.

    It writes the result into the graph state: the ticker, exchange, currency and
    company details when resolved; a question and candidates when the user must
    choose; or a reason when the company is not covered in version 1.

Why the model does only the extraction:
    Pulling a company out of free text is a language task, which models do well.
    Deciding which of 10,000 listings it is, is a lookup, which code does reliably
    and the same way every time (design 14.1).

How it is wired:
    make_entity_resolution_node() returns the node function. Its two helpers (the
    model call and the tool call) can be swapped for fakes, which is how the unit
    tests run without an API key or a server.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from importlib import resources

# langchain_core.messages: the standard message objects LangChain sends to any model.
from langchain_core.messages import HumanMessage, SystemMessage

# langchain_openai.ChatOpenAI: LangChain's wrapper around OpenAI's chat models.
from langchain_openai import ChatOpenAI

# pydantic: describes the exact shape of the answer we want back. Passing this class
# to .with_structured_output() makes the model return data in that shape, already
# parsed and type-checked, instead of free text we would have to pick apart.
from pydantic import BaseModel, Field

from invest_agent.config import get_settings
from invest_agent.graph.state import GraphState
from invest_agent.mcp_servers.clients import call_tool

logger = logging.getLogger(__name__)


class CompanyReference(BaseModel):
    """The model's answer. The Field descriptions are sent to the model as well."""

    company_reference: str | None = Field(
        default=None, description="The company named or described by the user, or null if none.")
    looks_like_ticker: bool = Field(
        default=False, description="True only if company_reference is written as a stock ticker.")
    chosen_option: int | None = Field(
        default=None, description="Number of the option picked from the list shown, or null.")
    full_name_guess: str | None = Field(
        default=None, description="Full official name of the company the user most likely means, or null.")


# Type names for the two swappable helpers, so the signatures read clearly.
ExtractFn = Callable[[str, list[dict]], Awaitable[CompanyReference]]
ResolveFn = Callable[[str, bool, str | None], Awaitable[dict]]


def _load_prompt() -> str:
    return resources.files("invest_agent.agents").joinpath("prompts/entity_extraction.txt").read_text(encoding="utf-8")


async def extract_with_model(message: str, options: list[dict]) -> CompanyReference:
    """The one Luna call: pull the company reference (and any choice) out of the message."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env; Entity Resolution needs it.")

    model = ChatOpenAI(
        model=settings.model_retrieval,
        api_key=settings.openai_api_key,
        max_retries=settings.max_model_retries,   # runaway protection: at most 2 retries
        timeout=30,
    ).with_structured_output(CompanyReference)

    text = message
    if options:
        # Show the options from the previous question, numbered, under the message.
        lines = [f"{i}. {o['name']} ({o['ticker']}, {o['exchange']}, {o['currency']})"
                 for i, o in enumerate(options, start=1)]
        text += "\n\nOptions shown to the user in the previous turn:\n" + "\n".join(lines)

    # ainvoke is the asynchronous call; it returns a CompanyReference object.
    return await model.ainvoke([SystemMessage(content=_load_prompt()), HumanMessage(content=text)])


async def resolve_with_server(query: str, confirmed: bool, full_name_guess: str | None = None) -> dict:
    """The MCP call to entity_server's resolve_ticker tool."""
    return await call_tool("entity", "resolve_ticker",
                           {"query": query, "confirmed": confirmed, "full_name_guess": full_name_guess})


def _exact_query(option: dict) -> str:
    """A query that can only match the chosen listing: "NSE:INFY" or "US:INFY"."""
    prefix = "NSE" if option["market"] == "IN" else "US"
    return f"{prefix}:{option['ticker']}"


def make_entity_resolution_node(extract: ExtractFn = extract_with_model,
                                resolve: ResolveFn = resolve_with_server):
    """Build the LangGraph node. Passing fakes for extract and resolve is for tests."""

    async def entity_resolution(state: GraphState) -> dict:
        message = state.get("raw_input", "")
        # Options offered last turn, only if last turn ended with a question.
        options = state.get("entity_candidates", []) if state.get("entity_status") == "ask_user" else []

        ref = await extract(message, options)

        # The user picked one of the offered options.
        if options and ref.chosen_option and 1 <= ref.chosen_option <= len(options):
            result = await resolve(_exact_query(options[ref.chosen_option - 1]), True, None)
        elif ref.company_reference:
            query = ref.company_reference.upper() if ref.looks_like_ticker else ref.company_reference
            # A ticker needs no guess; for a name, the model's full-name guess is
            # compared with the user's words by the matcher (added 5 October).
            guess = None if ref.looks_like_ticker else ref.full_name_guess
            result = await resolve(query, False, guess)
        elif state.get("entity_status") == "resolved" and state.get("ticker"):
            # A reply with no company in it (for example only the budget, answering the
            # profile question) keeps the company already resolved earlier in the chat.
            return {}
        else:
            return {
                "entity_status": "not_found",
                "entity_question": "Which company would you like me to look at? A name or a ticker is fine.",
                "entity_candidates": [],
            }

        return _to_state(result)

    return entity_resolution


def _to_state(result: dict) -> dict:
    """Map the tool's answer onto the graph state fields."""
    status = result["status"]
    update: dict = {
        "entity_status": status,
        "entity_candidates": result.get("candidates", []),
        "entity_question": result.get("question") or "",
    }
    listing = result.get("listing")
    if listing:
        update.update({
            "ticker": listing["ticker"],
            "exchange": listing["exchange"],
            "listing_currency": listing["currency"],
            "company_name": listing["name"],
            "market": listing["market"],
        })
        if listing.get("cik"):
            update["cik"] = str(listing["cik"])
        profile = result.get("sec_profile") or {}
        if profile.get("sic"):
            update["sic_code"] = profile["sic"]
    if status == "not_covered":
        update["not_covered_reason"] = result.get("reason") or ""
    if result.get("flags"):
        logger.info("Entity Resolution data flags: %s", result["flags"])
    return update


# The node the graph uses (step 4 wires it into build_graph.py).
entity_resolution_node = make_entity_resolution_node()