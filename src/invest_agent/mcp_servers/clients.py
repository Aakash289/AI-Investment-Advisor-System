"""
mcp_servers/clients.py: the one place that connects the app to its MCP servers.

What this file does:
    Lists every MCP server the app uses and how to start or reach it, and gives the
    agents one function to call a tool:

        result = await call_tool("entity", "resolve_ticker", {"query": "apple"})

    Our own servers (entity, market_data, filings, fx, macro_data and news) are started as child processes and spoken to over stdio. The two
    external servers (Trendlyne, MoSPI) will be added here as HTTP connections when
    their steps are built.

The library:
    langchain-mcp-adapters bridges MCP and LangChain. MultiServerMCPClient knows how
    to start or connect to each server; load_mcp_tools turns the server's tools into
    LangChain tool objects, which have an .ainvoke(arguments) method.

    Since step 14, a request keeps each server open from its first call to the end of
    the request (session_pool below), instead of starting it again for every call (A10).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from invest_agent.observability.tracked_call import tracked_call


def _python_server(module: str) -> dict:
    """Connection settings for one of our own servers, started as a child process.

    sys.executable is the Python running this app (the project's .venv), so the child
    uses the same libraries. env passes our environment through, because the MCP
    library otherwise gives child processes only a minimal set of variables; cwd keeps
    the project folder as the working folder, so the child finds .env and data/.
    """
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", module],
        "env": dict(os.environ),
        "cwd": os.getcwd(),
    }


SERVERS: dict[str, dict] = {
    "entity": _python_server("invest_agent.mcp_servers.entity.server"),
    "market_data": _python_server("invest_agent.mcp_servers.market_data.server"),
    "filings": _python_server("invest_agent.mcp_servers.filings.server"),
    "fx": _python_server("invest_agent.mcp_servers.fx.server"),
    "macro_data": _python_server("invest_agent.mcp_servers.macro_data.server"),
    "news": _python_server("invest_agent.mcp_servers.news.server"),
    # Added in later build steps:
    # "trendlyne":   {"transport": "streamable_http", "url": settings.trendlyne_mcp_url, ...},
    # "mospi":       {"transport": "streamable_http", "url": settings.mospi_mcp_url},
}

_client: MultiServerMCPClient | None = None


def get_client() -> MultiServerMCPClient:
    """One shared client object for the whole process."""
    global _client
    if _client is None:
        _client = MultiServerMCPClient(SERVERS)
    return _client


def _to_python(result: Any) -> Any:
    """Tool results arrive as text (JSON) or a list of content blocks, depending on the
    library version. Turn either into ordinary Python data."""
    if isinstance(result, tuple):                # (content, artifact) form
        result = result[0]
    if isinstance(result, list):                 # [{"type": "text", "text": "..."}]
        texts = [b.get("text", "") if isinstance(b, dict) else getattr(b, "text", str(b)) for b in result]
        result = "".join(texts)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


# --- Keeping servers open for one request (step 14, A10) -------------------------------
# Starting a server is a new Python process that imports pandas, edgartools and the
# rest, which takes a few seconds; the filings server alone is called 3 or 4 times in
# one analysis. Inside `async with session_pool():` (main.py wraps every request in
# one), each server is started the first time it is needed and kept open until the
# request ends, so later calls reuse it. Outside a pool (tests, one-off checks), each
# call starts and stops its own server as before.
#
# The MCP library opens a session with "async with", and its process handling
# insists that the block is left by the same task that entered it. Graph nodes run as
# separate tasks, so each server gets its own small "owner" task that opens the
# session, hands it over, and waits for the request to end before closing it.


class SessionPool:
    def __init__(self, client: MultiServerMCPClient) -> None:
        self._client = client
        self._sessions: dict[str, asyncio.Future] = {}
        self._tools: dict[str, dict] = {}
        self._close: dict[str, asyncio.Event] = {}
        self._owners: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    async def _owner(self, server: str, ready: asyncio.Future, close: asyncio.Event) -> None:
        try:
            async with self._client.session(server) as session:
                ready.set_result(session)
                await close.wait()
        except Exception as err:            # noqa: BLE001 - handed to the waiting caller
            if not ready.done():
                ready.set_exception(err)

    async def tools(self, server: str) -> dict:
        """The server's tools by name, starting the server the first time."""
        async with self._lock:
            if server not in self._sessions:
                ready = asyncio.get_running_loop().create_future()
                self._close[server] = asyncio.Event()
                self._sessions[server] = ready
                self._owners.append(asyncio.create_task(self._owner(server, ready, self._close[server])))
            ready = self._sessions[server]
        try:
            session = await asyncio.shield(ready)
        except Exception:
            await self.drop(server)
            raise
        if server not in self._tools:
            self._tools[server] = {t.name: t for t in await load_mcp_tools(session)}
        return self._tools[server]

    async def drop(self, server: str) -> None:
        """Close one server (after an error), so the next call starts it again."""
        self._sessions.pop(server, None)
        self._tools.pop(server, None)
        event = self._close.pop(server, None)
        if event:
            event.set()

    async def close(self) -> None:
        for event in self._close.values():
            event.set()
        await asyncio.gather(*self._owners, return_exceptions=True)
        self._sessions.clear()
        self._tools.clear()
        self._close.clear()

    @property
    def started(self) -> list[str]:
        return list(self._sessions)


_pool: contextvars.ContextVar[SessionPool | None] = contextvars.ContextVar("mcp_session_pool", default=None)


@asynccontextmanager
async def session_pool() -> AsyncIterator[SessionPool]:
    """Keep each MCP server open for the rest of the block (one request)."""
    pool = SessionPool(get_client())
    token = _pool.set(pool)
    try:
        yield pool
    finally:
        _pool.reset(token)
        await pool.close()


async def _call(server: str, tool: str, arguments: dict) -> Any:
    pool = _pool.get()
    if pool is not None:
        tools = await pool.tools(server)
        if tool not in tools:
            raise KeyError(f"Tool {tool!r} not found on MCP server {server!r}; it has {sorted(tools)}")
        try:
            return _to_python(await tools[tool].ainvoke(arguments))
        except (ConnectionError, BrokenPipeError, EOFError):
            await pool.drop(server)          # the server process went away; start it afresh next time
            raise
    # No pool: client.session(server) starts the server, and "async with" closes it
    # when the block ends.
    async with get_client().session(server) as session:
        tools = {t.name: t for t in await load_mcp_tools(session)}
        if tool not in tools:
            raise KeyError(f"Tool {tool!r} not found on MCP server {server!r}; it has {sorted(tools)}")
        return _to_python(await tools[tool].ainvoke(arguments))


async def call_tool(server: str, tool: str, arguments: dict, *, timeout: float = 60) -> Any:
    """Call one tool on one MCP server and return its result as Python data.

    Wrapped in tracked_call so the call is timed and logged like any other outside
    call. The timeout is longer than the 10-second default because starting the
    server process and loading reference lists takes a few seconds the first time;
    retries are off because the tool's own outside calls already retry.
    """
    return await tracked_call(f"mcp:{server}", tool, _call, server, tool, arguments,
                              quota_units=0, timeout=timeout, retries=0)
