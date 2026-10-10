"""
graph/build_graph.py: wires the nodes into one LangGraph graph.

What this file does:
    LangGraph runs the pipeline as a graph: nodes are the steps, edges say what runs
    next. This file adds every node and edge, following architecture_v9.mmd. After build
    step 13 the graph is:

        START
          |-- entity_resolution --|
          |-- profile_intake -----|   (both read the same message, in parallel)
                                  v
                             input_gate
          question: END /           \\ continue      (not covered: to compliance)
                             price, fundamentals, macro, news, geo_sector (parallel)
                                         v
                              currency_normalisation   (waits for all five)
                                         v
                                    verification
              not covered: compliance /    \\ continue
                                             analysis       (scores and verdict, step 9)
                                               v
                                           synthesis      (one Terra call, step 10)
                                               v
                                           grounding      (every number checked)
                                               v
                                             sizing       (amount and shares, step 11)
                                               v
                                         preview_output   (developer view, --detail)
                                               v
                                           compliance     (the answer and its disclaimer, step 12)
                                               v
                                             report       (the PDF, step 13)
                                               v
                                              END


    Steps 4 to 8 added the five retrieval agents, the FX step and verification; step 9
    added analysis, step 10 synthesis and the grounding guardrail, step 11 sizing,
    step 12 compliance and step 13 the PDF report.

Key LangGraph ideas used here:
    StateGraph(GraphState)   a graph whose shared state has the shape of GraphState
    add_node(name, fn)       register a step; fn receives the state and returns updates
    add_edge(a, b)           b runs after a
    add_edge([a, b], c)      c waits until both a and b have finished (a join)
    add_conditional_edges    pick the next node with a small function (route_after_gate)
    compile()                check the wiring and produce a runnable graph
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from invest_agent.agents.entity_resolution import entity_resolution_node
from invest_agent.agents.fundamentals import fundamentals_node
from invest_agent.agents.geo_sector import geo_sector_node
from invest_agent.agents.macro import macro_node
from invest_agent.agents.news import news_node
from invest_agent.agents.profile_intake import profile_intake_node
from invest_agent.agents.synthesis import synthesis_node
from invest_agent.agents.technical_price import technical_price_node
from invest_agent.analysis.step import analysis_node
from invest_agent.compliance.disclaimer import compliance_node
from invest_agent.decision.sizing import sizing_node
from invest_agent.fx.step import fx_node
from invest_agent.graph.nodes import (
    RETRIEVAL_AGENTS,
    input_gate,
    preview_output,
    route_after_gate,
    route_after_verification,
)
from invest_agent.graph.state import GraphState
from invest_agent.guardrails.grounding import grounding_node

# langgraph.graph: START and END are the built-in entry and exit points.
from invest_agent.observability.timings import timed
from invest_agent.reporting.step import report_node
from invest_agent.verification.step import verification_node


def build_graph(nodes: dict | None = None):
    """Build and compile the graph. `nodes` can replace any node by name (used in tests)."""
    n = {
        "entity_resolution": entity_resolution_node,
        "profile_intake": profile_intake_node,
        "input_gate": input_gate,
        "technical_price": technical_price_node,
        "fundamentals": fundamentals_node,
        "macro": macro_node,
        "news": news_node,
        "geo_sector": geo_sector_node,
        "currency_normalisation": fx_node,
        "verification": verification_node,
        "analysis": analysis_node,
        "synthesis": synthesis_node,
        "grounding": grounding_node,
        "sizing": sizing_node,
        "preview_output": preview_output,
        "compliance": compliance_node,
        "report": report_node,
    }
    n.update(nodes or {})

    graph = StateGraph(GraphState)
    for name, fn in n.items():
        # timed() records each step's duration for the developer view and the decision
        # log (step 14, A10); it does not change what the node does.
        graph.add_node(name, timed(name, fn))

    # Input layer: both agents start from the raw message at the same time.
    graph.add_edge(START, "entity_resolution")
    graph.add_edge(START, "profile_intake")
    # The gate waits for both (two edges in), then decides.
    graph.add_edge("entity_resolution", "input_gate")
    graph.add_edge("profile_intake", "input_gate")
    # route_after_gate returns either END or the list of retrieval agents to start;
    # a list makes LangGraph run them all at the same time.
    graph.add_conditional_edges("input_gate", route_after_gate, [END, "compliance", *RETRIEVAL_AGENTS])

    # Retrieval agents in parallel; the FX step waits for all of them. A list of start
    # nodes in add_edge makes a join: the FX step runs once, after every agent is done.
    graph.add_edge(list(RETRIEVAL_AGENTS), "currency_normalisation")
    graph.add_edge("currency_normalisation", "verification")
    # Verification can end the request (a company too small for version 1).
    graph.add_conditional_edges("verification", route_after_verification, ["compliance", "analysis"])
    # Analysis (9a: the sub-scores) runs on the verified figures, then the preview.
    graph.add_edge("analysis", "synthesis")
    # Synthesis (one Terra call) writes the reasoning; the guardrail checks every number.
    graph.add_edge("synthesis", "grounding")
    graph.add_edge("grounding", "sizing")
    # Sizing turns the checked verdict into an amount and a share count (step 11).
    graph.add_edge("sizing", "preview_output")
    # Compliance writes the answer the user reads and wraps it in the disclaimer (step 12).
    graph.add_edge("preview_output", "compliance")
    # The PDF report is drawn last, from the checked answer (step 13).
    graph.add_edge("compliance", "report")
    graph.add_edge("report", END)

    return graph.compile()


@lru_cache
def get_graph():
    """The compiled graph, built once per process."""
    return build_graph()
