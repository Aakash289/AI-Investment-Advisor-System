"""
invest_agent: investment suitability AI agent package.

What this file does:
    Python runs this file first whenever anything in invest_agent is imported, in the
    app, in every MCP server process, and in tests. It is used for one safety rule that
    must hold everywhere: secrets never reach the logs.

    The HTTP library (httpx) logs every request's full web address at INFO level, and
    some addresses carry a secret: Trendlyne's personal MCP URL has a token in it, and
    Massive and FMP take their API keys as address parameters. The MCP library turns
    INFO logging on by itself, so without this rule those secrets would print in the
    terminal (found on 5 October, when the Trendlyne token appeared in a test run).

    So here, before anything else runs:
      1. the HTTP loggers are set to WARNING, so request lines are not logged at all;
      2. every log line from every library is checked as it is created, and any
         token, apikey or api_key value is blanked out. This also covers error messages
         that quote a full web address (an HTTP error says "...for url 'https://...'"),
         and still works if someone lowers the logging level while debugging.
"""

import logging
import re

_SECRET = re.compile(r"((?:token|apikey|api_key|apiKey)=)[^&\s\"']+", re.IGNORECASE)


class RedactSecrets(logging.Filter):
    """Replace secret values in a log line with ***, then let the line through."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if _SECRET.search(message):
            record.msg, record.args = _SECRET.sub(r"\1***", message), None
        return True


for _name in ("httpx", "httpx2", "httpcore", "mcp.client.streamable_http"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# A "log record factory" is the function logging uses to create every log line. Wrapping
# it means each line is cleaned once, at the moment it is created, whatever logger or
# handler it later goes through.
_make_record = logging.getLogRecordFactory()
_redactor = RedactSecrets()


def _safe_record(*args, **kwargs) -> logging.LogRecord:
    record = _make_record(*args, **kwargs)
    try:
        _redactor.filter(record)
    except Exception:          # a badly formed log line must never stop the program
        pass
    return record


if not getattr(logging.getLogRecordFactory(), "_redacts_secrets", False):
    _safe_record._redacts_secrets = True       # mark it, so it is only installed once
    logging.setLogRecordFactory(_safe_record)