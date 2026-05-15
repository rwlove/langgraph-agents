"""Top-level fleet graph.

Phase 2: triager + note-maker + researcher are real nodes; the remaining 10
specialists still route to a `_pending` stub. Phase 8 replaces the rest of
the stubs with real nodes one at a time.

The graph entry point is the triager. After triage, the graph routes to the
target specialist based on `state.target_agent`.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from agents.nodes.note_maker import note_maker_node
from agents.nodes.researcher import researcher_node
from agents.nodes.triager import triager_node
from agents.state import ALL_AGENT_IDS, AgentId, FleetState

# Agent IDs that have a real node module wired below. Everything else still
# routes to the `_pending` stub.
_REAL_NODES: dict[str, str] = {
    "note-maker": "note-maker",
    "researcher": "researcher",
}


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
            f"STUB: triager routed to '{target}' (specialist not yet implemented "
            f"as of phase 2). Summary: {summary}"
        )
    }


def _route_after_triage(state: FleetState) -> AgentId | str:
    """Conditional edge: pick the target specialist (real node or stub)."""
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
    builder.add_node("note-maker", note_maker_node)
    builder.add_node("researcher", researcher_node)
    builder.add_node("_pending", _pending_specialist)

    # ---- edges ----
    builder.add_edge(START, "triager")

    # Build the routing map: real nodes go to their dedicated node;
    # everything else routes to the shared stub.
    route_map: dict[Hashable, str] = {}
    for agent in ALL_AGENT_IDS:
        if agent == "triager":
            continue
        route_map[agent] = _REAL_NODES.get(agent, "_pending")
    route_map[END] = END

    builder.add_conditional_edges("triager", _route_after_triage, route_map)
    builder.add_edge("note-maker", END)
    builder.add_edge("researcher", END)
    builder.add_edge("_pending", END)

    return builder.compile(checkpointer=checkpointer)
