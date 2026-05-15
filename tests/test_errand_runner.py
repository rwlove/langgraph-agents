"""errand-runner — approval verification + execution path."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
    state = FleetState(
        task_id="t-2",
        source="test",
        content="x",
        approval_request=ApprovalRequest(
            action_class="C",
            target="ha-mcp.call_service",
            payload_summary="restart frigate",
            undo_path="undo",
            proposed_by="smart-home-engineer",
        ),
        approval_granted=False,
    )
    result = errand_runner_node(state)
    assert "approval_granted is not True" in result["output"]


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
            proposed_by="smart-home-engineer",
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
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test-secret")
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
            proposed_by="smart-home-engineer",
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
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "test-secret")
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
            proposed_by="smart-home-engineer",
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
