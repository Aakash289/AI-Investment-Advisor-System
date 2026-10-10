"""
common/logging_setup.py: one logging format for the whole application.

What this file does:
    Python's built-in `logging` module is used everywhere (`logger =
    logging.getLogger(__name__)` at the top of a module, then `logger.info(...)`).
    This file configures it once, at start-up: the level comes from LOG_LEVEL in
    `.env`, and in a terminal the output is coloured and aligned by the `rich`
    library. Call `setup_logging()` once from `main.py` or an MCP server's start-up.
"""

import logging

# rich is a library for nicer terminal output. Its RichHandler is a drop-in logging
# handler that adds colours, timestamps, and readable tracebacks. Console(stderr=True)
# sends log lines to standard error: our MCP servers use standard output to talk to
# the app, so a log line written there would break the conversation.
from rich.console import Console
from rich.logging import RichHandler

from invest_agent.config import get_settings

_configured = False


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger once. Later calls do nothing."""
    global _configured
    if _configured:
        return

    level_name = (level or get_settings().log_level).upper()

    # basicConfig attaches the handler to the root logger, so every module's logger
    # (created with logging.getLogger(__name__)) inherits this format and level.
    logging.basicConfig(
        level=level_name,
        format="%(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=Console(stderr=True), rich_tracebacks=True, show_path=False)],
    )

    # The HTTP libraries log every request at INFO, which drowns our own messages;
    # keep them at WARNING unless we are debugging.
    if level_name != "DEBUG":
        for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "yfinance", "openai", "mcp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True