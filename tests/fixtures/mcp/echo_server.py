"""
tests/fixtures/mcp/echo_server.py: a tiny MCP server for the session pool tests.

What this file does:
    One tool, whoami, that returns this server process's id. Two calls that get the same
    id went to the same running server; different ids mean the server was started twice.
    Started by the tests as a child process over stdio, like the app's own servers.
"""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def whoami() -> dict:
    return {"pid": os.getpid()}


if __name__ == "__main__":
    mcp.run()
