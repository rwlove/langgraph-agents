"""Top-level fleet graph.

Phase 8: all 13 agents have real nodes. The `_pending` stub is gone — any
routing target must resolve to a registered node.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from agents.nodes.coder import coder_node
from agents.nodes.errand_runner import errand_runner_node
from agents.nodes.health_tracker import health_tracker_node
from agents.nodes.homelab_engineer import homelab_engineer_node
from agents.nodes.ml_tuner import ml_tuner_node
from agents.nodes.note_maker import note_maker_node
from agents.nodes.property_coordinator import property_coordinator_node
from agents.nodes.reporter import reporter_node
from agents.nodes.researcher import researcher_node
from agents.nodes.reviewer import reviewer_node
from agents.nodes.smart_home_engineer import smart_home_engineer_node
from agents.nodes.supervisor import supervisor_node
from agents.nodes.triager import triager_node
from agents.state import ALL_AGENT_IDS, AgentId, FleetState


def _route_after_triage(state: FleetState) -> AgentId | str:
    """Conditional edge from the triager."""
    target = state.target_agent
    if target is None or target == "triager":
        return END
    return target


def _route_after_specialist(state: FleetState) -> AgentId | str:
    """Conditional edge from a specialist that may have set a rejection.

    If the specialist rejected, route to supervisor. Otherwise END (or, in
    future phases, route to errand-runner if an approval flow was triggered).
    """
    if state.rejection is not None:
        return "supervisor"
    return END


def _route_after_supervisor(state: FleetState) -> AgentId | str:
    """Supervisor either reroutes (new target_agent) or escalates (END)."""
    target = state.target_agent
    if target is None or target == "supervisor":
        return END
    # If supervisor mutated target_agent to a specialist, route there.
    return target


def build_fleet_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    """Build + compile the fleet graph.

    Pass a checkpointer in production (PostgresSaver). Pass None in tests to
    run without persistence.
    """
    builder = StateGraph(FleetState)

    # ---- nodes (13) ----
    builder.add_node("triager", triager_node)
    builder.add_node("reporter", reporter_node)
    builder.add_node("note-maker", note_maker_node)
    builder.add_node("researcher", researcher_node)
    builder.add_node("coder", coder_node)
    builder.add_node("errand-runner", errand_runner_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("reviewer", reviewer_node)
    builder.add_node("homelab-engineer", homelab_engineer_node)
    builder.add_node("smart-home-engineer", smart_home_engineer_node)
    builder.add_node("ml-tuner", ml_tuner_node)
    builder.add_node("health-tracker", health_tracker_node)
    builder.add_node("property-coordinator", property_coordinator_node)

    # ---- edges ----
    builder.add_edge(START, "triager")

    # Triager → one of 12 specialists (or END if triager is somehow the target)
    triage_route_map: dict[Hashable, str] = {
        agent: agent for agent in ALL_AGENT_IDS if agent != "triager"
    }
    triage_route_map[END] = END
    builder.add_conditional_edges("triager", _route_after_triage, triage_route_map)

    # Each specialist routes either to supervisor (on rejection) or END.
    specialist_route_map: dict[Hashable, str] = {"supervisor": "supervisor", END: END}
    for agent in ALL_AGENT_IDS:
        if agent in ("triager", "supervisor"):
            continue
        builder.add_conditional_edges(agent, _route_after_specialist, specialist_route_map)

    # Supervisor either reroutes to a specialist or escalates to END.
    supervisor_route_map: dict[Hashable, str] = {
        agent: agent for agent in ALL_AGENT_IDS if agent not in ("triager", "supervisor")
    }
    supervisor_route_map[END] = END
    builder.add_conditional_edges("supervisor", _route_after_supervisor, supervisor_route_map)

    return builder.compile(checkpointer=checkpointer)
