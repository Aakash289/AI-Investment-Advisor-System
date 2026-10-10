"""
common/yaml_loader.py: loads the YAML configuration tables that ship inside the package.

What this file does:
    Many decisions in the design are numbers and lists that should be easy to change
    without touching code: match thresholds, suitability thresholds, trusted news
    outlets, coverage rules, and so on. They live in .yaml files next to the code that
    uses them (for example `analysis/scorecard_config.yaml`). This module gives every
    other module one function, `load_yaml("analysis/scorecard_config.yaml")`, that
    finds the file inside the installed package, parses it once, and returns a dict.

Why read from the package and not the current folder:
    The app can be started from any working directory (a terminal, a test runner, an
    MCP server process). Locating files relative to the `invest_agent` package always
    finds the right copy.
"""

from functools import lru_cache
from importlib import resources
from typing import Any

# PyYAML parses YAML text into Python dicts and lists. safe_load only builds plain
# data types, never arbitrary Python objects, which is the safe choice for config.
import yaml

PACKAGE = "invest_agent"


class ConfigFileError(RuntimeError):
    """Raised when a config file is missing or does not contain a YAML mapping."""


# lru_cache keeps each parsed file in memory after the first read, so calling
# load_yaml in a hot loop costs nothing. The returned dict is shared, so callers must
# treat it as read-only (copy it before changing anything).
@lru_cache(maxsize=None)
def load_yaml(relative_path: str) -> dict[str, Any]:
    """Return the parsed contents of a YAML file inside the invest_agent package.

    relative_path is relative to src/invest_agent/, using forward slashes,
    for example "mcp_servers/entity/entity_config.yaml".
    """
    # importlib.resources.files() returns a path-like object for the installed
    # package, which works whether the package is a plain folder or installed by uv.
    file = resources.files(PACKAGE).joinpath(relative_path)
    if not file.is_file():
        raise ConfigFileError(f"Config file not found inside the package: {relative_path}")

    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigFileError(f"Config file must contain a YAML mapping at the top level: {relative_path}")
    return data
