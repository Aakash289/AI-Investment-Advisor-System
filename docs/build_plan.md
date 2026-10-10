# Investment Suitability Agent: Build Plan (v1, superseded)

> **Superseded.** This was the first plan, written before the build started (early
> October 2026). The agent as built is described in the [README](../README.md),
> [architecture.md](architecture.md) and [findings.md](findings.md). Some choices below
> changed while building: the PDF is drawn with ReportLab, not WeasyPrint; the scorecard
> weights are 50/20/15/5/10; models are called about four times a request, not ten. It is
> kept as a record of how the design started.

This document covers the decisions behind the codebase: which models to use and why,
why LangGraph plus LangChain plus custom MCP servers, what each file is for, how the
environment is configured, and the phased order to actually build and test this in.
It assumes the uv-managed project already scaffolded alongside this document.

## 1. Model selection, with cost and latency in mind

The pipeline makes roughly ten LLM calls per request: entity resolution, five retrieval
agents running in parallel, the synthesis agent, and the grounding check. Using one
model everywhere would either overpay on the simple calls or underpower the one call
that actually needs to reason. The project runs entirely on OpenAI models, chosen for
cost. Current per-million-token pricing:

| Model | Input | Output | Cached input | Batch (in/out) |
|---|---|---|---|---|
| GPT-5.6 Luna | $0.20 | $1.20 | $0.02 | 50% off both |
| GPT-5.6 Terra | $2.00 | $12.00 | $0.20 | 50% off both |
| GPT-5.6 Sol | $5.00 | $30.00 | $0.50 | 50% off both |

Assignment:

- Entity resolution and the five retrieval agents (fundamentals, technical, macro,
  news, geo/sector): GPT-5.6 Luna. These mostly call a tool and lightly structure
  what comes back, and they fire in parallel every request, so this is where cost and
  latency add up fastest and where the least reasoning depth is needed. At roughly a
  fifth of the next tier's price, this is the main source of the cost saving from
  moving off Claude.
- Synthesis agent: GPT-5.6 Terra. This is the one node doing real reasoning across
  five inputs, worth the stronger tier. Worth noting for the record: Claude Sonnet 5
  was actually marginally cheaper than Terra on this specific node ($10 vs $12 output
  per million tokens), so the cost saving from this particular model comes entirely
  from the retrieval layer, not from synthesis. The project is on OpenAI throughout
  for operational simplicity, one provider, one SDK, one set of credentials.
- Grounding guardrail: GPT-5.6 Luna, since this is closer to verification than open
  reasoning. A meaningful part of this check (does this number appear in the logged
  sources) is plain Python matching, not an LLM call at all.
- Verification, completeness scoring, position sizing, and the concentration check:
  no LLM call. These are deterministic logic and should be implemented as plain
  Python functions, not prompts.
- Backtesting harness: runs offline, outside the live request path, so it is the
  right candidate for the Batch API's 50% discount.
- Prompt caching: the tool schemas and system instructions repeated across the five
  parallel retrieval agents should be cached, since this is the largest chunk of
  repeated input tokens, and cached input on Luna is a tenth of its already low rate.

## 2. Orchestration: LangGraph, LangChain, and MCP

- **LangGraph** is the orchestration layer itself. The architecture is already a state
  machine: parallel fan-out to five agents, conditional edges on the grounding and
  concentration checks, a shared state object flowing through every node. LangGraph's
  StateGraph (see `src/invest_agent/graph/state.py` for the shared state schema) maps
  onto this directly, including native parallel branch execution.
- **LangChain** is not the orchestrator, it is the glue inside individual nodes.
  Specifically, `langchain-mcp-adapters` converts the custom MCP server tools into
  LangChain tool objects a LangGraph node can bind and call, through `langchain-openai`
  for the model binding. LangChain's own agent executor is not needed here, since a
  manual tool-call loop (while loop, call model, check for tool use, execute, feed
  back) is exactly what LangGraph formalizes.
- **MCP** wraps each external data source. Community MCP servers for financial filings,
  macro data, and news either do not exist or are not trustworthy enough to use
  unmodified, so these are custom-built using the official `mcp` Python SDK (FastMCP),
  grouped by category rather than one server per API so there are not ten separate
  processes to manage:
  - `filings_server`: SEC EDGAR and NSE/BSE (Tier 1 primary), Financial Modeling Prep
    and Alpha Vantage (Tier 2 fallback), as separate tools in the same server
  - `market_data_server`: Yahoo Finance (primary, no key needed) and Polygon (fallback)
  - `macro_data_server`: FRED, World Bank, IMF, RBI
  - `news_server`: NewsAPI, GDELT, Reuters
  - `fx_server`: currency conversion

## 3. File structure, and what each piece is for

```
investment-suitability-agent/
├── .env.example            template for all required environment variables
├── .env                     actual secrets, gitignored, never committed
├── .gitignore
├── .python-version          pins Python 3.12 for uv
├── pyproject.toml           dependencies, managed by uv (uv sync / uv add)
├── README.md
├── docs/
│   └── build_plan.md        this document
├── src/invest_agent/
│   ├── config.py             loads .env into a typed Settings object, read by every
│   │                         other module instead of each one reading os.environ
│   ├── graph/
│   │   ├── state.py          the shared GraphState TypedDict that flows through every
│   │   │                     LangGraph node, one field per architecture layer's output
│   │   └── build_graph.py    wires the nodes and edges into the actual StateGraph
│   ├── agents/                entity resolution, the five retrieval agents, synthesis,
│   │                         one file each, each calling its MCP tools through
│   │                         langchain-mcp-adapters
│   ├── verification/          cross-source conflict resolution and the completeness
│   │                         scorer, plain Python, no LLM call
│   ├── fx/                    currency normalization, plain Python
│   ├── guardrails/            the grounding check: cross-references synthesis output
│   │                         against the decision log's source records
│   ├── decision/              position sizing (budget and risk profile into a specific
│   │                         currency amount) and the concentration check, plain Python
│   ├── compliance/            the disclaimer wrapper applied to every output
│   ├── mcp_servers/           the five custom MCP servers described in Section 2
│   ├── observability/         the cost/latency tracker (instruments every LLM and MCP
│   │                         call) and the decision log (the audit trail)
│   ├── cache/                 fundamentals and news caching with TTL and event-triggered
│   │                         invalidation
│   └── eval/                  the offline backtesting harness
└── tests/
    ├── smoke/                 one test per external dependency (see Section 5)
    ├── unit/                  one test per node's internal logic, external calls mocked
    └── integration/           the full graph, run end to end against real or recorded data
```

## 4. Environment configuration

`.env.example` lists every variable the app reads, grouped by what it configures:
model names per node, data source credentials (API keys where needed, the SEC's
required User-Agent header where it is not a key), the cost and latency budget
thresholds the tracker enforces, and the cache TTLs. Copy it to `.env` and fill in
keys as they are obtained, nothing needs to be filled in all at once: a smoke test
exists for each key and will skip cleanly if that key is not set yet, rather than
failing confusingly deeper in the pipeline.

No separate `requirements.txt` is used, uv manages dependencies through
`pyproject.toml` plus the `uv.lock` file it generates. `uv sync` installs everything,
`uv add <package>` adds a new dependency and updates the lock file.

## 5. Build phases

Each phase should be fully working and tested before moving to the next one. Building
the orchestrator before any single data source is confirmed reachable is the most
common way these projects stall.

**Phase 0: environment setup.** `uv sync`, copy `.env.example` to `.env`, get an
OpenAI API key at minimum. Data source keys can be added incrementally.

**Phase 1: smoke test every external dependency in isolation.** Run
`uv run pytest tests/smoke -v -m smoke`. This confirms the OpenAI API is reachable
with both models, SEC EDGAR responds, yfinance returns data, and any keys already in
`.env` work, before a single line of agent logic is written. If something fails here,
it is a credentials or network problem, not a bug in the agent.

**Phase 2: build the MCP servers, one at a time.** Start with `market_data_server`
since it needs no key. Each server should be smoke-testable on its own by starting it
and calling its tool directly, before it is wired into the graph.

**Phase 3: the smallest possible graph.** Entity resolution plus one retrieval agent
(fundamentals), wired into a two-node LangGraph graph, tested end to end with a
hardcoded ticker like AAPL. This proves the LangGraph plus langchain-mcp-adapters
plumbing actually works before adding complexity.

**Phase 4: add the remaining four retrieval agents as parallel branches.** Confirm they
actually run in parallel (check wall-clock time, not just that results come back).

**Phase 5: currency normalization, verification, and completeness scoring.** These are
plain Python, test them with unit tests against fabricated conflicting records before
relying on real data.

**Phase 6: synthesis agent.** This is the first node where prompt quality actually
matters, expect to iterate on the prompt template (5-year trend as the anchor, news as
a modifier) more than once.

**Phase 7: grounding guardrail.**

**Phase 8: decision layer.** Position sizing and the concentration check, both plain
Python, both unit-testable with fabricated verdicts and budgets.

**Phase 9: compliance wrapper and final output formatting.**

**Phase 10: cost and latency tracker**, instrumented across every call site already
built in phases 3 through 9.

**Phase 11: caching layer.**

**Phase 12: decision log and the offline backtesting harness.** This can run well after
the live pipeline works, it does not block Phase 1 through 11.

**Phase 13: integration tests and hardening.** Run the full graph against several real
tickers across both US and Indian markets, check the output against the requirements
and risk documents directly, confirm the budget threshold actually short-circuits when
artificially lowered, confirm the grounding check actually catches a deliberately
fabricated number.

## 6. Testing strategy

Three tiers, matching the `tests/` folder structure:

- **Smoke tests** (`tests/smoke/`): one file per external dependency, each testing
  exactly one thing, a model responds, an API is reachable. No orchestration, no mocks.
  These are the first thing to run in a new environment and the first thing to check
  when something breaks, since they isolate whether a problem is external (a key
  expired, a provider is down) or internal (a bug in the agent logic).
- **Unit tests** (`tests/unit/`): one file per node's internal logic, with every
  external call mocked. This is where the deterministic logic (verification,
  completeness scoring, position sizing, concentration check) gets most of its
  coverage, since it is cheap to test exhaustively without hitting real APIs.
- **Integration tests** (`tests/integration/`): the full graph, run against real or
  recorded API responses, confirming the pieces actually work together, not just in
  isolation.
