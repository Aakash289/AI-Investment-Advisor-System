"""
graph/state.py: the shared state object that flows through every node of the graph.

What this file does:
    LangGraph runs the pipeline as a graph of nodes (entity resolution, the five
    retrieval agents, verification, analysis, synthesis, guardrail, sizing, report,
    compliance). The nodes never call each other directly. Instead each node receives
    the current state, reads the fields it needs, and returns a dict with only the
    fields it changed; LangGraph merges that dict back into the state.

    This file declares every field in that state, grouped by the layer of
    architecture_v9.mmd that writes it. It also declares the record types that
    several layers share, the most important being SourceRecord: every retrieved
    number travels as a SourceRecord so it can always be traced to its source, which
    is what the grounding guardrail (14.12) and the report's source list rely on.

Design references: Section 7 (architecture layers), 15.2 (the fields v9 needs).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict

# ---------------------------------------------------------------------------------
# Small shared vocabularies. Literal types make a typo (for example "favorable"
# instead of "favourable") show up as an error in an editor or type checker.
# ---------------------------------------------------------------------------------

Verdict = Literal["favourable", "neutral", "unfavourable", "insufficient_data", "not_covered"]
Confidence = Literal["high", "medium", "low"]
RiskTolerance = Literal["low", "medium", "high"]
Goal = Literal["growth", "income", "capital_preservation"]
Currency = Literal["INR", "USD"]

# Source tiers from Section 8: 1 = official filings and regulators, 2 = licensed or
# established aggregators, 3 = news and media.
SourceTier = Literal[1, 2, 3]


# ---------------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------------

class SourceRecord(TypedDict):
    """One retrieved data point, always kept together with where it came from.

    Required keys come first; NotRequired keys are filled when the source has them
    (for example accession_number only exists for SEC filings).
    """

    record_id: str               # stable ID cited in the reasoning, e.g. "sec:AAPL:revenue:FY2024"
    field: str                   # our metric name, e.g. "revenue", "close_price", "policy_rate"
    value: Any                   # usually a number; text for labels; None never stored (missing stays missing)
    source: str                  # provider, e.g. "sec_edgar", "trendlyne", "yfinance", "fred"
    tier: SourceTier
    as_of: str                   # ISO date or timestamp the value refers to or was fetched
    unit: NotRequired[str]       # "INR", "USD", "percent", "shares", "ratio"
    entity: NotRequired[str]     # ticker or country code the value is about
    fiscal_year: NotRequired[int]
    period_end: NotRequired[str]
    accession_number: NotRequired[str]   # SEC filing ID
    source_ref: NotRequired[str]         # Trendlyne source ID, URL, or series ID
    release_date: NotRequired[str]       # when a macro figure was published
    projected: NotRequired[bool]         # True only for Forecasting Agent output
    flags: NotRequired[list[str]]        # e.g. ["stale", "units_corrected"]


class UserProfile(TypedDict, total=False):
    """The six profile and budget fields from intake (14.2). Every field can be
    missing (total=False) because a message often carries only some of them."""

    risk_tolerance: RiskTolerance
    horizon_years: float
    goal: Goal
    concentration_limit_pct: float       # the user's own limit, as a percentage of budget
    liquidity_needs: bool                # may need the money back at short notice
    budget_amount: float                 # total investable money, not money for this stock
    budget_currency: Currency
    budget_currency_assumed: bool        # True when the currency came from the default rule
    assumed_fields: list[str]            # fields filled by defaults after asking once
    target_return_pct: float             # a return target in the question, in percent (not stored)
    target_return_basis: str             # "per_year" or "total"
    target_years: float                  # the period the target is for


class CallRecord(TypedDict):
    """One outside call (API, MCP tool, or model), written by tracked_call (step 1)
    and the cost tracker (step 14). Feeds the decision log's cost and latency view."""

    service: str                 # "sec_edgar", "tavily", "openai:gpt-5.6-terra", ...
    operation: str               # e.g. "company_facts", "search"
    started_at: str
    duration_s: float
    ok: bool
    attempts: int
    quota_units: int
    cost_usd: float
    error: NotRequired[str]


# ---------------------------------------------------------------------------------
# The graph state
# ---------------------------------------------------------------------------------
# total=False means a node only has to return the keys it changed.
#
# Annotated[list[...], operator.add] is LangGraph's "reducer" syntax. When several
# nodes run in parallel (the five retrieval agents) and each returns, say,
# {"call_log": [...]}, LangGraph combines the lists with operator.add (list + list)
# instead of letting the last node to finish overwrite the others.

class GraphState(TypedDict, total=False):
    # --- Request ---
    request_id: str
    started_at: str
    raw_input: str                       # the user's message, exactly as typed

    # --- Input layer: Entity Resolution (14.1) ---
    entity_status: Literal["resolved", "ask_user", "not_found", "not_covered"]
    entity_question: str                 # asked when the match is unclear or cross-listed
    entity_candidates: list[dict]
    ticker: str
    exchange: str
    listing_currency: Currency
    company_name: str
    cik: str                             # SEC company ID, US only
    sic_code: int
    market: Literal["US", "IN"]
    not_covered_reason: str              # 20-F filer, REIT or InvIT, or small company

    # --- Input layer: Profile and Budget Intake (14.2) ---
    user_profile: UserProfile
    missing_profile_fields: list[str]
    followup_question: str
    followup_asked: bool                 # the ask-once rule
    budget_needs_confirmation: bool      # ranges and mixed units only

    # --- Retrieval layer (each agent appends its own records) ---
    fundamentals_records: Annotated[list[SourceRecord], operator.add]
    price_records: Annotated[list[SourceRecord], operator.add]
    price_chart: dict                    # weekly closes, up to 5 years, stock and benchmark (report chart)
    macro_records: Annotated[list[SourceRecord], operator.add]
    news: dict                           # labelled items, filings, net sentiment (agents/news.py)
    macro_panel: dict                    # six economies with indicators and regime labels (agents/macro.py)
    news_records: Annotated[list[SourceRecord], operator.add]
    geo_sector_records: Annotated[list[SourceRecord], operator.add]
    bank_mode: bool                      # banks, NBFCs, insurers (14.3)
    sector: str                          # one of our 11 sectors (agents/geo_sector.py), for the macro fit score
    trendlyne_sector: str                # Trendlyne's own sector label (India), from the Fundamentals agent
    industry: str                        # Trendlyne's industry (India), used for Indian peers
    geo_exposure: dict                   # revenue share by panel economy, its source and checks (agents/geo_sector.py)
    peer_tickers: list[str]
    peer_group_level: str                # "sic4", "sic3", "sic2", or "trendlyne_industry"

    # --- Currency normalisation (14.8) ---
    fx_records: Annotated[list[SourceRecord], operator.add]
    fx_flags: list[str]
    fx: dict                             # rate used, budget converted, risk note, notes (fx/step.py)

    # --- Verification layer (14.9) ---
    verified_records: list[SourceRecord]
    sanity_flags: list[dict]
    conflicts: list[dict]
    completeness_score: float
    completeness_detail: dict
    amount_withheld: bool
    amount_withheld_reason: str
    valuation: dict                      # multiples and peer medians from E5
    data_notes: list[str]

    # --- Analysis layer (14.10, 14.11, 14.18) ---
    sub_scores: dict[str, float | None]  # quality, valuation, macro, news, forecast
    findings: list[dict]                 # plain-language findings with their record IDs
    analysis_detail: dict                # per sub-score: components, used weight, what was missing
    projection: dict                     # low, base, high cases, tagged projected
    fair_value: dict
    projection_records: list[SourceRecord]   # every projected figure, tagged "projected" (14.10)
    scorecard: dict
    suitability_notes: list[str]
    preliminary_verdict: Verdict
    preliminary_confidence: Confidence

    # --- Synthesis (14.11) ---
    facts: list[dict]                    # numbered facts F1, F2... the only things Synthesis may cite
    synthesis: dict                      # the model's answer after the verdict checks, before grounding
    verdict: Verdict
    confidence: Confidence
    verdict_change_reason: str
    reasoning: list[dict]                # paragraphs, each with the record IDs it cites
    key_risks: list[dict]
    what_would_change_the_view: list[str]

    # --- Guardrail layer (14.12) ---
    guardrail_log: list[dict]
    removed_sentences: list[str]
    guardrail_flags: list[str]

    # --- Decision layer (14.13) ---
    sizing: dict                         # status, amount, shares, notes (14.13)
    sizing_steps: list[dict]
    recommended_amount: float
    recommended_currency: Currency
    recommended_shares: int
    overexposure_note: str
    liquidity_note: str

    # --- Report and compliance (14.14, 14.15) ---
    report_path: str
    report_error: str
    disclaimer_version: str
    situation_lines: list[str]
    phrase_removals: list[dict]          # sentences removed for a banned phrase (14.15)
    preview_response: str                # the developer view, shown with --detail
    final_response: str

    # --- Cross-cutting: tracking and audit (14.16, 14.19) ---
    call_log: Annotated[list[CallRecord], operator.add]
    partial_analysis: bool
    skipped_steps: list[str]
    decision_log_id: str
