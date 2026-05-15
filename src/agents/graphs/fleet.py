"""Top-level fleet graph.

Phase 8: all 13 agents have real nodes. The `_pending` stub is gone — any
routing target must resolve to a registered node.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Hashable
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from agents.nodes.coder import coder_node
from agents.nodes.doc_writer import doc_writer_node
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
from agents.state import ALL_AGENT_IDS, ActionClass, AgentId, FleetState
from agents.tools.activity_log import log_activity

logger = logging.getLogger("agents.graphs.fleet")


# Per-agent action class default. errand-runner can produce C/D depending on
# the approval class it was invoked with; the rest are A (read-only side-
# effect-free wrt MCP) or B (vault-draft write). The activity log records
# this for security-review cat 8 audit traceability.
_DEFAULT_ACTION_CLASS: dict[str, ActionClass] = {
    "triager": "A",
    "reporter": "A",
    "note-maker": "B",
    "researcher": "A",
    "coder": "B",
    "errand-runner": "C",  # nominal; the node itself records the actual class executed
    "supervisor": "A",
    "reviewer": "A",
    "homelab-engineer": "A",
    "smart-home-engineer": "A",
    "ml-tuner": "A",
    "health-tracker": "C",  # every health-tracker output is approval-gated
    "property-coordinator": "B",
    "doc-writer": "B",
}


def _with_activity_log(
    agent_id: AgentId, fn: Callable[[FleetState], dict[str, Any]]
) -> Any:
    """Wrap a node so each invocation logs to the per-agent activity log.

    Best-effort: if the log write fails (vault unmounted, perms, etc.) we
    log a warning but never raise — the agent run shouldn't die over an
    audit-trail write failure.

    Return type is `Any` (via cast) because LangGraph's `add_node` overload
    set uses internal `_Node` protocols that a plain Callable doesn't match
    under strict mypy. The runtime accepts any callable.
    """
    def wrapper(state: FleetState) -> dict[str, Any]:
        result = fn(state)
        try:
            output = str(result.get("output", "") or "")[:200]
            log_activity(
                agent_id,
                state.task_id,
                action_class=_DEFAULT_ACTION_CLASS.get(agent_id, "A"),
                summary=output or "(no output)",
                outcome="success" if "CANCELLED" not in output else "error",
            )
        except Exception as exc:
            logger.warning("activity log write failed for %s: %s", agent_id, exc)
        return result
    wrapper.__name__ = fn.__name__
    return cast(Any, wrapper)


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
    builder.add_node("triager", _with_activity_log("triager", triager_node))
    builder.add_node("reporter", _with_activity_log("reporter", reporter_node))
    builder.add_node("note-maker", _with_activity_log("note-maker", note_maker_node))
    builder.add_node("researcher", _with_activity_log("researcher", researcher_node))
    builder.add_node("coder", _with_activity_log("coder", coder_node))
    builder.add_node("errand-runner", _with_activity_log("errand-runner", errand_runner_node))
    builder.add_node("supervisor", _with_activity_log("supervisor", supervisor_node))
    builder.add_node("reviewer", _with_activity_log("reviewer", reviewer_node))
    builder.add_node(
        "homelab-engineer", _with_activity_log("homelab-engineer", homelab_engineer_node)
    )
    builder.add_node(
        "smart-home-engineer",
        _with_activity_log("smart-home-engineer", smart_home_engineer_node),
    )
    builder.add_node("ml-tuner", _with_activity_log("ml-tuner", ml_tuner_node))
    builder.add_node(
        "health-tracker", _with_activity_log("health-tracker", health_tracker_node)
    )
    builder.add_node(
        "property-coordinator",
        _with_activity_log("property-coordinator", property_coordinator_node),
    )
    builder.add_node("doc-writer", _with_activity_log("doc-writer", doc_writer_node))

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
