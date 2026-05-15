"""Top-level fleet graph.

Phase 1: triager-only. Specialists land in phase 8.

The graph entry point is the triager. After triage, the graph routes to the
target specialist based on `state.target_agent`. In phase 1, all specialist
routes hit a `pending` node that records the routing decision and ends
gracefully — useful for smoke-testing the triager end-to-end without the
specialists existing yet.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from agents.nodes.triager import triager_node
from agents.state import ALL_AGENT_IDS, AgentId, FleetState


def _pending_specialist(state: FleetState) -> dict[str, Any]:
    """Placeholder for specialist nodes that haven't been authored yet.

    Records the routing decision in state.output so the smoke test confirms
    the triager classified correctly. Phase 8 replaces this with real
    specialist nodes one at a time.
    """
    triage = state.triage
    summary = triage.summary if triage else "no triage available"
    target = state.target_agent or "unknown"
    return {
        "output": (
            f"PHASE-1 STUB: triager routed to '{target}' "
            f"(specialist not yet implemented). Summary: {summary}"
        )
    }


def _route_after_triage(state: FleetState) -> AgentId | str:
    """Conditional edge: pick the target specialist (all stubbed in phase 1)."""
    target = state.target_agent
    if target is None or target == "triager":
        return END
    return target


def build_fleet_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    """Build + compile the fleet graph.

    Pass a checkpointer in production (PostgresSaver). Pass None in tests to
    run without persistence (in-memory-only single invocation).
    """
    builder = StateGraph(FleetState)

    # ---- nodes ----
    builder.add_node("triager", triager_node)

    # Phase 1 stub: one shared `_pending` node serves every specialist target.
    builder.add_node("_pending", _pending_specialist)

    # ---- edges ----
    builder.add_edge(START, "triager")

    # All specialist targets route to the stub in phase 1.
    route_map: dict[Hashable, str] = {
        agent: "_pending"
        for agent in ALL_AGENT_IDS
        if agent != "triager"
    }
    route_map[END] = END

    builder.add_conditional_edges("triager", _route_after_triage, route_map)
    builder.add_edge("_pending", END)

    return builder.compile(checkpointer=checkpointer)
