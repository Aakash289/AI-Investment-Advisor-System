"""
config.py: the one place that reads settings for the whole application.

What this file does:
    Every API key, model name, limit, cache lifetime and file path the app needs is
    declared here as a typed field. The values come from environment variables, which
    are normally loaded from the `.env` file at the project root (see `.env.example`
    for every variable with an explanation). Every other module calls
    `get_settings()` instead of reading `os.environ` directly, so there is exactly one
    place that knows the variable names.

Design references: Section 15.1 (services and keys), 14.16 (limits and quotas),
14.19 (storage paths and retention).
"""

from functools import lru_cache

# pydantic-settings is an add-on to the pydantic validation library. A class that
# inherits from BaseSettings reads each field from an environment variable with the
# same name (case does not matter), converts it to the declared type (str, int,
# float), and raises a clear error if a value cannot be converted.
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # model_config tells pydantic-settings where to look:
    #   env_file=".env"     read variables from the .env file as well as the real environment
    #   extra="ignore"      unknown variables in .env are ignored instead of causing an error
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Models (OpenAI) ---
    # Keys default to an empty string so settings load even before you add them;
    # the code that needs a key checks for it and gives a clear message.
    openai_api_key: str = ""
    model_retrieval: str = "gpt-5.6-luna"   # entity extraction, profile intake, news labelling
    model_synthesis: str = "gpt-5.6-terra"  # the one reasoning call per request
    model_guardrail: str = "gpt-5.6-luna"   # the meaning check in the grounding guardrail
    # How long the quick Luna calls think before answering (speed, 7 October): news
    # labelling and the meaning check are sorting tasks, so they use "low" effort. Left
    # empty, the model's own default is used. If the model refuses the setting, the call is
    # simply made again without it (common/models.py).
    fast_reasoning_effort: str = "low"
    # The Terra call that writes the reasoning (10 October): empty uses the model's default,
    # which is what every run so far used. Try "low" or "medium" to compare speed and quality.
    synthesis_reasoning_effort: str = ""

    # --- US filings and fundamentals ---
    sec_edgar_user_agent: str = ""          # SEC requires "Your Name your-email@example.com"
    financial_modeling_prep_api_key: str = ""

    # --- Indian fundamentals (Trendlyne MCP Pro, provisional until the first-month checks) ---
    trendlyne_mcp_url: str = ""
    trendlyne_api_key: str = ""

    # --- Prices ---
    polygon_api_key: str = ""               # Massive (formerly Polygon) free plan, US fallback

    # --- Macro ---
    fred_api_key: str = ""
    mospi_mcp_url: str = "https://mcp.mospi.gov.in/"

    # --- News ---
    tavily_api_key: str = ""

    # --- Latency limit and runaway protection (no dollar cap; cost is only logged) ---
    max_latency_per_request_seconds: int = 120
    max_graph_steps: int = 50               # LangGraph recursion limit per request
    max_model_retries: int = 2              # retries per model call

    # --- Outside calls (decided 4 October: 10-second timeout, 2 retries, then fallback) ---
    tool_timeout_seconds: float = 10.0
    tool_retries: int = 2

    # --- Free-plan quotas ---
    tavily_monthly_credit_limit: int = 1000
    trendlyne_monthly_call_limit: int = 2000
    trendlyne_daily_call_limit: int = 400
    fmp_daily_call_limit: int = 250
    quota_warn_fraction: float = 0.8

    # --- Cache lifetimes ---
    fundamentals_cache_ttl_hours: int = 24
    quote_cache_ttl_minutes: int = 15
    macro_cache_ttl_hours: int = 24
    segment_cache_ttl_days: int = 7
    fx_cache_ttl_hours: int = 24
    peer_cache_ttl_hours: int = 24
    news_cache_ttl_minutes: int = 60

    # --- Storage ---
    database_path: str = "data/invest_agent.sqlite"
    reports_dir: str = "data/reports"
    decision_log_retention_days: int = 730

    # --- App ---
    app_env: str = "development"
    log_level: str = "INFO"


# lru_cache makes get_settings() build the Settings object once and then return the
# same object on every later call, so the .env file is read only once per process.
# Tests that change environment variables can call get_settings.cache_clear().
@lru_cache
def get_settings() -> Settings:
    return Settings()
