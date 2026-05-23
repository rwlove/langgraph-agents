"""errand-runner — the only agent with MCP write capability.

Tight propose-then-execute contract: every Class C+ action MUST arrive with
a valid signed approval token (issued by Windmill's approval-broker after Rob's
Zulip reaction). The node verifies the token + pre-flight checks BEFORE any
MCP write call.

Hard constraints (architectural):
- No `git push` to home-ops without homelab-engineer's proposal
- No VPN-gateway operations (LAN-only per security review)
- No medical-system writes — health-tracker is read-only by design
- No personal-vault writes
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.types import interrupt
from pydantic import BaseModel

from agents.llm import llm
from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ActionClass, AgentId, FleetState
from agents.tools.mcp import MCPGatewayClient, MCPPermissionError, is_allowed

_AGENT_ID: AgentId = "errand-runner"
_TEMPERATURE = 0.0  # deterministic — we're verifying + executing, not generating

Outcome = Literal["executed", "rejected", "preflight-failed", "no-approval", "deferred"]


class ExecutionResult(BaseModel):
    outcome: Outcome
    reason: str
    server: str | None = None
    method: str | None = None
    payload_hash: str | None = None


def _payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a payload — for audit logging without storing the payload itself."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _verify_approval_token(
    token: str,
    task_id: str,
    action_class: ActionClass,
    server: str,
    method: str,
    *,
    signing_secret: str,
) -> bool:
    """HMAC-SHA256 verification. Expected payload: task_id|class|server|method|<nonce>.

    Windmill's approval-broker mints these. The same secret lives in 1Password and
    is injected into both this pod and the Windmill worker. Phase 4 of the
    redesign wires it; for now, treat the token format as the contract.
    """
    if not token or ":" not in token:
        return False
    payload, signature = token.rsplit(":", 1)
    parts = payload.split("|")
    if len(parts) < 5:  # task_id|class|server|method|nonce
        return False
    p_task, p_class, p_server, p_method, *_ = parts
    if (p_task, p_class, p_server, p_method) != (task_id, action_class, server, method):
        return False
    expected = hmac.new(
        signing_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _build_llm() -> BaseChatModel:
    """Used only when the inbound request needs interpretation. Most invocations
    bypass the LLM and go straight to verify + execute."""
    return llm(_AGENT_ID, temperature=_TEMPERATURE)


def errand_runner_node(state: FleetState) -> dict[str, Any]:  # noqa: PLR0911
    """Verify + execute a proposed MCP-write action.

    Required state fields:
      - approval_request: ApprovalRequest with target = "server.method"
      - approval_token: signed token from Windmill (or from the /approval resume)
      - approval_granted: True (or this node refuses)

    If no approval_request is set, the request was misrouted — reject.

    Approval lifecycle inside this node:
      - approval_granted is None    → pause via ``interrupt(req.model_dump())``;
        ``/approval`` resumes with ``{granted, deferred, approval_token, actor}``.
      - resume verdict ``deferred`` → return a deferred-output message (the
        supervisor can re-route or escalate later).
      - resume verdict ``granted=False`` → return rejected-output message.
      - resume verdict ``granted=True`` → fall through to the existing
        token-verification + execute path.
    """
    persona = load_persona(_AGENT_ID)  # noqa: F841  # available for future LLM-interpreted flows
    settings = get_settings()

    req = state.approval_request
    if req is None:
        return {
            "output": (
                "errand-runner: no approval_request in state. Specialist must "
                "propose the action first; supervisor should route here only "
                "after the approval flow."
            ),
        }

    # Approval verdict resolution.
    #
    # If `approval_granted` is still unset (None), we pause here and let the
    # /approval HTTPRoute resume us with the user's verdict. On resume,
    # LangGraph re-executes the node from the top — every check above is
    # idempotent (validation + load_persona/get_settings only), so re-running
    # them is safe.
    #
    # If `approval_granted` is already True/False at node entry (set elsewhere
    # — e.g. a pre-approved test path), skip the interrupt and fall through
    # to the existing logic.
    granted: bool
    approval_token: str | None
    if state.approval_granted is None:
        verdict: dict[str, Any] = interrupt(req.model_dump())
        # Resume payload shape (api/approval.py post_approval):
        #   {granted: bool, deferred: bool, approval_token: str, actor: str}
        if verdict.get("deferred"):
            result = ExecutionResult(
                outcome="deferred",
                reason=(
                    f"deferred by {verdict.get('actor', 'unknown')}; "
                    "supervisor may re-route or escalate."
                ),
            )
            return {"output": f"errand-runner: {result.reason}"}
        granted = bool(verdict.get("granted", False))
        approval_token = verdict.get("approval_token")
    else:
        granted = bool(state.approval_granted)
        approval_token = state.approval_token

    if not granted:
        result = ExecutionResult(
            outcome="rejected",
            reason="approval not granted; refusing.",
        )
        return {"output": f"errand-runner: {result.reason}"}

    # Decompose target into server + method (specialist proposes "server.method")
    if "." not in req.target:
        return {
            "output": (
                f"errand-runner: malformed target '{req.target}'. "
                "Expected 'server.method' (e.g. 'ha-mcp.call_service')."
            ),
        }
    server, method = req.target.split(".", 1)

    # Static scope check — errand-runner is the only writer per mcp.py allowlist
    if not is_allowed(_AGENT_ID, server, method):
        return {
            "output": (
                f"errand-runner: server.method '{req.target}' not in allowlist. "
                "Add it to tools/mcp.py:ALLOWLISTS if intended."
            ),
        }

    # Signed-token verification — shared HMAC with Windmill's approval-receive workflow.
    # `approval_token` is sourced from the resume verdict if we paused here,
    # otherwise from inbound state (back-compat with pre-approved test paths).
    signing_secret = settings.langgraph_approval_signing_key or ""
    if not _verify_approval_token(
        approval_token or "",
        task_id=state.task_id,
        action_class=req.action_class,
        server=server,
        method=method,
        signing_secret=signing_secret,
    ):
        return {
            "output": (
                f"errand-runner: approval token invalid for "
                f"{state.task_id}|{req.action_class}|{server}|{method}"
            ),
        }

    # Pre-flight: undo_path is required for Class C; absent → escalate to D.
    if req.undo_path is None and req.action_class == "C":
        return {
            "output": (
                "errand-runner: Class C action missing undo_path. Escalate "
                "to Class D for explicit confirmation."
            ),
        }

    # Execute the MCP call.
    payload: dict[str, Any] = {
        "task_id": state.task_id,
        "proposed_by": req.proposed_by,
        "summary": req.payload_summary,
    }
    payload_hash = _payload_hash(payload)
    try:
        with MCPGatewayClient(_AGENT_ID) as client:
            mcp_result = client.call(server, method, arguments=payload)
        result = ExecutionResult(
            outcome="executed",
            reason=f"{server}.{method} returned {mcp_result.status_code}",
            server=server,
            method=method,
            payload_hash=payload_hash,
        )
    except MCPPermissionError as exc:
        result = ExecutionResult(
            outcome="rejected",
            reason=str(exc),
            server=server,
            method=method,
            payload_hash=payload_hash,
        )
    except Exception as exc:  # MCPCallError + httpx errors
        result = ExecutionResult(
            outcome="preflight-failed",
            reason=f"MCP call failed: {exc}",
            server=server,
            method=method,
            payload_hash=payload_hash,
        )

    return {
        "output": (
            f"errand-runner: {result.outcome} {server}.{method} "
            f"(class={req.action_class}, hash={payload_hash}) — {result.reason}"
        ),
    }
