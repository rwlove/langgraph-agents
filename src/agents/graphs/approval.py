"""Approval subgraph: agent proposes → interrupt → resume on user reaction.

LangGraph's `interrupt()` pauses execution. The checkpointer persists state to
Postgres. The caller (FastAPI `/inbox` handler) returns the pause info to n8n,
which posts the approval request to Zulip + Pushover.

When the user reacts (👍 / 👎 / ⏸️), n8n's approval-broker hits `/approval`,
which calls `graph.invoke(Command(resume=...))` to continue execution from the
exact point where it paused.

The subgraph is reusable: any specialist can invoke it before a Class C/D
action by populating `state.approval_request`.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.state import FleetState


def _prepare_approval(state: FleetState) -> dict[str, Any]:
    """Validate the approval request before pausing.

    Specialists are expected to populate `state.approval_request` before
    routing through this subgraph. If they didn't, that's a bug — skip the
    interrupt and bounce.
    """
    if state.approval_request is None:
        return {"approval_granted": False}
    return {}


def _wait_for_user(state: FleetState) -> dict[str, Any]:
    """Pause until n8n delivers the user's reaction via `/approval`.

    `interrupt()` surfaces the request payload to the caller. When resumed,
    the value passed to `Command(resume=...)` becomes the return value here.
    """
    payload = {
        "task_id": state.task_id,
        "approval_request": state.approval_request.model_dump() if state.approval_request else None,
    }
    reaction: dict[str, Any] = interrupt(payload)  # paused here until resumed

    granted = reaction.get("granted", False)
    token = reaction.get("approval_token")
    return {
        "approval_granted": granted,
        "approval_token": token,
    }


def _record_outcome(state: FleetState) -> dict[str, Any]:
    """Bookkeeping after approval resolves; no state changes needed in phase 1."""
    return {}


def build_approval_subgraph() -> Any:
    """Construct (but do not compile — the parent graph compiles with its checkpointer)."""
    builder = StateGraph(FleetState)
    builder.add_node("prepare", _prepare_approval)
    builder.add_node("wait", _wait_for_user)
    builder.add_node("record", _record_outcome)

    def _short_circuit_if_no_request(state: FleetState) -> str:
        return "record" if state.approval_request is None else "wait"

    builder.add_edge(START, "prepare")
    builder.add_conditional_edges(
        "prepare",
        _short_circuit_if_no_request,
        {"wait": "wait", "record": "record"},
    )
    builder.add_edge("wait", "record")
    builder.add_edge("record", END)
    return builder
