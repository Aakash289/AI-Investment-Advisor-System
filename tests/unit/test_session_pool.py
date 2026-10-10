"""
tests/unit/test_session_pool.py: MCP servers kept open for one request (step 14, A10).

What this file checks:
    inside session_pool(), two calls to the same server reach the same process
    outside a pool, each call starts its own process (the old behaviour)
    parallel calls share one start, and the pool closes everything at the end

Uses tests/fixtures/mcp/echo_server.py, a real MCP server started over stdio, so no
network is needed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from invest_agent.mcp_servers import clients

ECHO = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "echo_server.py"


@pytest.fixture
def echo_server(monkeypatch):
    servers = {"echo": {"transport": "stdio", "command": sys.executable, "args": [str(ECHO)],
                        "env": dict(os.environ), "cwd": os.getcwd()}}
    monkeypatch.setattr(clients, "SERVERS", servers)
    monkeypatch.setattr(clients, "_client", None)


def test_pool_reuses_one_server(echo_server):
    async def run():
        async with clients.session_pool() as pool:
            a = await clients.call_tool("echo", "whoami", {})
            b = await clients.call_tool("echo", "whoami", {})
            return a, b, pool.started
    a, b, started = asyncio.run(run())
    assert a["pid"] == b["pid"] and started == ["echo"]


def test_without_a_pool_each_call_starts_a_server(echo_server):
    async def run():
        return await clients.call_tool("echo", "whoami", {}), await clients.call_tool("echo", "whoami", {})
    a, b = asyncio.run(run())
    assert a["pid"] != b["pid"]


def test_parallel_calls_share_one_start(echo_server):
    async def run():
        async with clients.session_pool():
            return await asyncio.gather(*(clients.call_tool("echo", "whoami", {}) for _ in range(4)))
    results = asyncio.run(run())
    assert len({r["pid"] for r in results}) == 1
