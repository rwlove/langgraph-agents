"""Top-level fleet graph.

Phase 8: all 13 agents have real nodes. The `_pending` stub is gone — any
routing target must resolve to a registered node.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Hashable
from typing import Any, cast

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore

from agents.nodes import NODES
from agents.observability import get_logger
from agents.state import ALL_AGENT_IDS, ActionClass, AgentId, FleetState
from agents.tools.activity_log import log_activity

logger = logging.getLogger("agents.graphs.fleet")
slog = get_logger("graphs.fleet")


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
    "network-operator": "A",  # propose-only; class C+ side effects route via errand-runner
    "storage-operator": "A",  # propose-only; class C+ side effects route via errand-runner
    "smart-home-operator": "A",  # propose-only; class C+ side effects route via errand-runner
    "ml-operator": "A",  # propose-only; class C+ side effects route via errand-runner
    "observability-operator": "A",  # propose-only; class C+ side effects route via errand-runner
    "health-tracker": "C",  # every health-tracker output is approval-gated
    "property-coordinator": "B",
    "doc-writer": "B",
}


def _with_activity_log(agent_id: AgentId, fn: Callable[[FleetState], dict[str, Any]]) -> Any:
    """Wrap a node so each invocation logs to the per-agent activity log.

    Best-effort: if the log write fails (vault unmounted, perms, etc.) we
    log a warning but never raise — the agent run shouldn't die over an
    audit-trail write failure.

    Return type is `Any` (via cast) because LangGraph's `add_node` overload
    set uses internal `_Node` protocols that a plain Callable doesn't match
    under strict mypy. The runtime accepts any callable.
    """

    def wrapper(state: FleetState) -> dict[str, Any]:
        # Bind `agent` on structlog contextvars so any structlog event the
        # node emits carries the agent label. The outer /inbox or /approval
        # handler bound task_id; the {agent, task_id, event} triple is what
        # the Loki dashboard trail viewer joins on. Token-based unbind on the
        # way out is essential — LangGraph runs nodes serially in the same
        # asyncio task, so without it the next node inherits the previous
        # node's `agent` label.
        token = structlog.contextvars.bind_contextvars(agent=agent_id)
        slog.info("node_start")
        t0 = time.perf_counter()
        try:
            result = fn(state)
        except Exception as exc:
            slog.warning(
                "node_error",
                error_type=type(exc).__name__,
                duration_s=time.perf_counter() - t0,
            )
            structlog.contextvars.reset_contextvars(**token)
            raise
        duration_s = time.perf_counter() - t0
        output = str(result.get("output", "") or "")[:200]
        outcome = "success" if "CANCELLED" not in output else "error"
        slog.info(
            "node_end",
            outcome=outcome,
            output_preview=output[:80],
            duration_s=duration_s,
        )
        try:
            log_activity(
                agent_id,
                state.task_id,
                action_class=_DEFAULT_ACTION_CLASS.get(agent_id, "A"),
                summary=output or "(no output)",
                outcome=outcome,
            )
        except Exception as exc:
            logger.warning("activity log write failed for %s: %s", agent_id, exc)
        structlog.contextvars.reset_contextvars(**token)
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

    Precedence (first match wins):
      1. ``rejection`` set → supervisor (the supervisor re-routes or escalates).
      2. ``approval_request`` set + ``target_agent == "errand-runner"`` →
         errand-runner. This is the propose-then-execute path: the specialist
         composed an ApprovalRequest, errand-runner pauses on ``interrupt()``,
         the /approval HTTPRoute resumes with the user's verdict.
      3. otherwise → END.

    The errand-runner branch is what makes PR #36's interrupt path
    reachable end-to-end. Without it, specialists that populated
    ``approval_request`` would END before errand-runner ever ran.
    """
    if state.rejection is not None:
        return "supervisor"
    if state.approval_request is not None and state.target_agent == "errand-runner":
        return "errand-runner"
    return END


def _route_after_supervisor(state: FleetState) -> AgentId | str:
    """Supervisor either reroutes (new target_agent) or escalates (END)."""
    target = state.target_agent
    if target is None or target == "supervisor":
        return END
    # If supervisor mutated target_agent to a specialist, route there.
    return target


def build_fleet_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: BaseStore | None = None,
) -> Any:
    """Build + compile the fleet graph.

    Args:
      checkpointer: per-thread short-term state. PostgresSaver in prod,
        MemorySaver / None in tests.
      store: long-term cross-agent KG store (MCPMemoryStore in prod).
        Passed through to `compile(store=...)`; agent nodes that ask
        their `RunnableConfig` for `store` get this back. None disables
        long-term store access.
    """
    builder = StateGraph(FleetState)

    # ---- nodes (registered from the NODES dict; adding a new agent is now
    # one Literal entry + one node file + one NODES dict entry — no edits here) ----
    for agent_id, node_fn in NODES.items():
        builder.add_node(agent_id, _with_activity_log(agent_id, node_fn))

    # ---- edges ----
    builder.add_edge(START, "triager")

    # Triager → one of 12 specialists (or END if triager is somehow the target)
    triage_route_map: dict[Hashable, str] = {
        agent: agent for agent in ALL_AGENT_IDS if agent != "triager"
    }
    triage_route_map[END] = END
    builder.add_conditional_edges("triager", _route_after_triage, triage_route_map)

    # Each specialist routes to supervisor (rejection), errand-runner
    # (approval composition), or END. errand-runner is excluded from this
    # loop and wired directly to END below — re-applying the same router
    # post-execute would re-trigger on its still-set approval_request and
    # loop the node back into itself.
    specialist_route_map: dict[Hashable, str] = {
        "supervisor": "supervisor",
        "errand-runner": "errand-runner",
        END: END,
    }
    for agent in ALL_AGENT_IDS:
        if agent in ("triager", "supervisor", "errand-runner"):
            continue
        builder.add_conditional_edges(agent, _route_after_specialist, specialist_route_map)
    builder.add_edge("errand-runner", END)

    # Supervisor either reroutes to a specialist or escalates to END.
    supervisor_route_map: dict[Hashable, str] = {
        agent: agent for agent in ALL_AGENT_IDS if agent not in ("triager", "supervisor")
    }
    supervisor_route_map[END] = END
    builder.add_conditional_edges("supervisor", _route_after_supervisor, supervisor_route_map)

    return builder.compile(checkpointer=checkpointer, store=store)
