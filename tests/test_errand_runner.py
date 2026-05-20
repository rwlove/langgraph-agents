"""errand-runner — approval verification + execution path."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from agents.nodes.errand_runner import errand_runner_node
from agents.settings import get_settings
from agents.state import ApprovalRequest, FleetState


def _signed_token(
    task_id: str, action_class: str, server: str, method: str, secret: str, nonce: str = "abc"
) -> str:
    payload = f"{task_id}|{action_class}|{server}|{method}|{nonce}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def test_refuses_when_no_approval_request(temp_vault: Path) -> None:
    state = FleetState(task_id="t-1", source="test", content="x", approval_granted=True)
    result = errand_runner_node(state)
    assert "no approval_request in state" in result["output"]


def test_refuses_when_approval_not_granted(temp_vault: Path) -> None:
    """approval_granted=False (explicit reject already in state) skips the
    interrupt path and returns a rejected-output message."""
    state = FleetState(
        task_id="t-2",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="restart frigate",
            undo_path="undo",
            proposed_by="smart-home-operator",
        ),
        approval_granted=False,
    )
    result = errand_runner_node(state)
    assert "approval not granted" in result["output"]


def test_refuses_malformed_target(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-3",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="not_a_valid_target",  # missing the dot
            payload_summary="x",
            undo_path="undo",
            proposed_by="coder",
        ),
        approval_granted=True,
    )
    result = errand_runner_node(state)
    assert "malformed target" in result["output"]


def test_refuses_out_of_scope_call(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-4",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="kubectl-mcp.apply",  # not in errand-runner's allowlist (apply not allowed)
            payload_summary="x",
            undo_path="undo",
            proposed_by="homelab-engineer",
        ),
        approval_granted=True,
    )
    result = errand_runner_node(state)
    assert "not in allowlist" in result["output"]


def test_refuses_invalid_signature(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-5",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="x",
            undo_path="undo",
            proposed_by="smart-home-operator",
        ),
        approval_granted=True,
        approval_token="forged-token-no-sig",
    )
    result = errand_runner_node(state)
    assert "approval token invalid" in result["output"]


def test_class_c_without_undo_path_escalates(
    temp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Class C action that lacks an undo path should be bumped to D for explicit
    confirmation rather than silently proceeding."""
    monkeypatch.setenv("LANGGRAPH_APPROVAL_SIGNING_KEY", "test-secret")
    get_settings.cache_clear()

    token = _signed_token("t-6", "C", "ha-mcp", "call_service", "test-secret")
    state = FleetState(
        task_id="t-6",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="x",
            undo_path=None,  # no undo!
            proposed_by="smart-home-operator",
        ),
        approval_granted=True,
        approval_token=token,
    )
    result = errand_runner_node(state)
    assert "missing undo_path" in result["output"]


def test_executes_when_all_gates_pass(
    temp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: valid approval + valid signature + undo path + allowlist match."""
    monkeypatch.setenv("LANGGRAPH_APPROVAL_SIGNING_KEY", "test-secret")
    get_settings.cache_clear()

    token = _signed_token("t-7", "C", "ha-mcp", "call_service", "test-secret")
    state = FleetState(
        task_id="t-7",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="turn on porch light",
            undo_path="ha-mcp.call_service light.turn_off",
            proposed_by="smart-home-operator",
        ),
        approval_granted=True,
        approval_token=token,
    )

    # Mock the MCP gateway HTTP call
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"status": "ok"}
    fake_response.text = '{"status":"ok"}'
    with patch("httpx.Client.post", return_value=fake_response):
        result = errand_runner_node(state)
    assert "executed ha-mcp.call_service" in result["output"]


# ---------------------------------------------------------------------------
# interrupt() pause/resume cycle.
#
# P1.2: when an ApprovalRequest arrives without a pre-resolved
# `approval_granted`, the node MUST pause on `interrupt(req.model_dump())`
# until the /approval HTTPRoute resumes it with the user's verdict.
#
# These tests build a minimal one-node graph around `errand_runner_node`
# with an in-memory checkpointer. The first invoke surfaces the
# `__interrupt__` payload; the second invoke (with `Command(resume=...)`)
# carries the verdict back into the node.
# ---------------------------------------------------------------------------


def _build_errand_only_graph() -> Any:
    """Tiny graph: START → errand_runner → END, with MemorySaver checkpointer."""
    builder = StateGraph(FleetState)
    builder.add_node("errand-runner", errand_runner_node)
    builder.add_edge(START, "errand-runner")
    builder.add_edge("errand-runner", END)
    return builder.compile(checkpointer=MemorySaver())


def test_interrupt_pauses_when_approval_granted_unset(temp_vault: Path) -> None:
    """approval_granted=None (unset) pauses the node on interrupt(req)."""
    graph = _build_errand_only_graph()
    config = {"configurable": {"thread_id": "t-int-1"}}

    initial = FleetState(
        task_id="t-int-1",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="turn on porch light",
            undo_path="ha-mcp.call_service light.turn_off",
            proposed_by="smart-home-operator",
        ),
        # approval_granted intentionally unset → interrupt path
    )

    result = graph.invoke(initial, config=config)
    # Pause surfaces an `__interrupt__` key in the returned partial state.
    assert "__interrupt__" in result
    interrupts = result["__interrupt__"]
    assert len(interrupts) == 1
    # The interrupt value is the dumped ApprovalRequest.
    iv = interrupts[0].value
    assert iv["target"] == "ha-mcp.call_service"
    assert iv["action_class"] == "C"
    assert iv["proposed_by"] == "smart-home-operator"

    # State snapshot also reflects the pause via tasks[].interrupts.
    snap = graph.get_state(config)
    assert snap.tasks and snap.tasks[0].interrupts


def test_interrupt_resume_granted_proceeds_to_execute(
    temp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume with granted=True + a valid signed token → executes the MCP call."""
    monkeypatch.setenv("LANGGRAPH_APPROVAL_SIGNING_KEY", "test-secret")
    get_settings.cache_clear()

    graph = _build_errand_only_graph()
    config = {"configurable": {"thread_id": "t-int-2"}}
    token = _signed_token("t-int-2", "C", "ha-mcp", "call_service", "test-secret")

    initial = FleetState(
        task_id="t-int-2",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="turn on porch light",
            undo_path="ha-mcp.call_service light.turn_off",
            proposed_by="smart-home-operator",
        ),
    )

    # First invoke → pause.
    paused = graph.invoke(initial, config=config)
    assert "__interrupt__" in paused

    # Resume with granted verdict + a valid token (this is the shape
    # api/approval.py's post_approval sends).
    resume_value = {
        "granted": True,
        "deferred": False,
        "approval_token": token,
        "actor": "rob",
    }
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"status": "ok"}
    fake_response.text = '{"status":"ok"}'
    with patch("httpx.Client.post", return_value=fake_response):
        final = graph.invoke(Command(resume=resume_value), config=config)

    assert "executed ha-mcp.call_service" in final["output"]


def test_interrupt_resume_rejected_returns_rejected_output(temp_vault: Path) -> None:
    """Resume with granted=False → rejected-output message; no MCP call."""
    graph = _build_errand_only_graph()
    config = {"configurable": {"thread_id": "t-int-3"}}

    initial = FleetState(
        task_id="t-int-3",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="x",
            undo_path="undo",
            proposed_by="smart-home-operator",
        ),
    )
    paused = graph.invoke(initial, config=config)
    assert "__interrupt__" in paused

    resume_value = {
        "granted": False,
        "deferred": False,
        "approval_token": "",
        "actor": "rob",
    }
    # If we accidentally proceed to MCP execution, this patch would fail
    # loudly — but we expect to short-circuit before the call.
    with patch("httpx.Client.post", side_effect=AssertionError("must not call MCP")):
        final = graph.invoke(Command(resume=resume_value), config=config)

    assert "approval not granted" in final["output"]


def test_interrupt_resume_deferred_returns_deferred_output(temp_vault: Path) -> None:
    """Resume with deferred=True → deferred-output; supervisor takes over later."""
    graph = _build_errand_only_graph()
    config = {"configurable": {"thread_id": "t-int-4"}}

    initial = FleetState(
        task_id="t-int-4",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="x",
            undo_path="undo",
            proposed_by="smart-home-operator",
        ),
    )
    paused = graph.invoke(initial, config=config)
    assert "__interrupt__" in paused

    resume_value = {
        "granted": False,
        "deferred": True,
        "approval_token": "",
        "actor": "rob",
    }
    with patch("httpx.Client.post", side_effect=AssertionError("must not call MCP")):
        final = graph.invoke(Command(resume=resume_value), config=config)

    assert "deferred by rob" in final["output"]
