"""errand-runner — the only agent with MCP write capability.

Tight propose-then-execute contract: every Class C+ action MUST arrive with
a valid signed approval token (issued by Windmill's approval-broker after Rob's
Zulip reaction). The node verifies the token + pre-flight checks BEFORE any
MCP write call.

Hard constraints (architectural):
- No `git push` to home-ops without homelab-engineer's proposal
- No VPN-gateway operations (LAN-only per security review)
- No medical-system writes — health-tracker is read-only by design
- No personal-vault writes — except the smoke-test marker under
  `settings.vault_smoke_dir` (filesystem-only path; see _run_smoke).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ActionClass, AgentId, FleetState
from agents.tools.mcp import MCPGatewayClient, MCPPermissionError, is_allowed

_AGENT_ID: AgentId = "errand-runner"
_TEMPERATURE = 0.0  # deterministic — we're verifying + executing, not generating

# Smoke-test pseudo-server. Any approval_request.target with this prefix is
# intercepted before the MCPGatewayClient call and routed to the in-process
# filesystem smoke implementation. See _run_smoke.
_SMOKE_SERVER = "smoke"

Outcome = Literal["executed", "rejected", "preflight-failed", "no-approval", "deferred"]


class ExecutionResult(BaseModel):
    outcome: Outcome
    reason: str
    server: str | None = None
    method: str | None = None
    payload_hash: str | None = None


class SmokeTimings(BaseModel):
    """Per-step millisecond timings for the smoke-execution path."""

    hmac_verify_ms: float = 0.0
    fs_write_ms: float = 0.0
    fs_readback_ms: float = 0.0
    fs_delete_ms: float = 0.0
    smoke_total_ms: float = 0.0


class SmokeResult(BaseModel):
    """Self-verifying smoke outcome — emitted as JSON in `state.output`.

    `hmac_verify_ok` is the load-bearing assertion. The smoke test exists to
    catch silent drift between Windmill's signing key/algorithm and this
    node's verifier; if hmac_verify_ok is True, the two ends agree.
    """

    path: str
    expected_content: str
    actual_content: str | None = None
    write_ok: bool = False
    readback_ok: bool = False
    delete_ok: bool = False
    file_gone_after_delete: bool = False
    hmac_verify_ok: bool = False
    timings: SmokeTimings = Field(default_factory=SmokeTimings)


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


def _run_smoke(
    state: FleetState,
    *,
    hmac_verify_ms: float,
    hmac_ok: bool,
) -> dict[str, Any]:
    """Filesystem self-verifying smoke for the approval flow.

    Triggered by `approval_request.target == "smoke.test_write"`. Bypasses
    the MCPGatewayClient entirely — the goal is to validate the production
    HMAC-verify path and the LangGraph interrupt/resume cycle, NOT to test
    a real MCP server. See graphs/smoke.py for the entry-point rationale.

    Path safety: writes ONLY to ``<vault_smoke_dir>/smoke-<task_id>.md``.
    The directory is created on first use; the filename is derived from
    state.task_id (the only operator-supplied input that reaches here).
    All other path components are constants from settings — there is no
    way for the caller to redirect the write outside vault_smoke_dir.

    The result is JSON-serialized into `state.output` so the
    `langgraph-smoke-approval-flow` Windmill workflow can read it back via
    `/admin/tasks/<id>` and surface timings + pass/fail per step.
    """
    settings = get_settings()
    smoke_dir = Path(settings.vault_smoke_dir)
    smoke_dir.mkdir(parents=True, exist_ok=True)

    # task_id is constrained by the API (single-line string, length-limited).
    # Reject anything path-shaped defensively — same `Path.name` discipline
    # the activity-log writer uses.
    safe_id = Path(state.task_id).name
    target_path = smoke_dir / f"smoke-{safe_id}.md"
    expected_content = (
        f"smoke-test marker\ntask_id: {safe_id}\nwritten_by: errand-runner\n"
    )

    result = SmokeResult(
        path=str(target_path),
        expected_content=expected_content,
        hmac_verify_ok=hmac_ok,
        timings=SmokeTimings(hmac_verify_ms=hmac_verify_ms),
    )

    smoke_start = time.perf_counter()

    # 1) write
    write_start = time.perf_counter()
    try:
        target_path.write_text(expected_content, encoding="utf-8")
        result.write_ok = True
    except OSError:
        result.write_ok = False
    result.timings.fs_write_ms = (time.perf_counter() - write_start) * 1000.0

    # 2) readback
    if result.write_ok:
        readback_start = time.perf_counter()
        try:
            actual = target_path.read_text(encoding="utf-8")
            result.actual_content = actual
            result.readback_ok = actual == expected_content
        except OSError:
            result.readback_ok = False
        result.timings.fs_readback_ms = (time.perf_counter() - readback_start) * 1000.0

    # 3) delete (always attempt, even if write/readback failed — cleanup)
    delete_start = time.perf_counter()
    try:
        target_path.unlink(missing_ok=True)
        result.delete_ok = True
    except OSError:
        result.delete_ok = False
    result.timings.fs_delete_ms = (time.perf_counter() - delete_start) * 1000.0

    # 4) verify file is actually gone
    result.file_gone_after_delete = not target_path.exists()

    result.timings.smoke_total_ms = (time.perf_counter() - smoke_start) * 1000.0

    # Serialize the full result envelope into state.output. The Windmill
    # driver reads this back via `/admin/tasks/<id>` and surfaces it to the
    # operator. JSON keeps the schema explicit; prose would lose timings.
    return {"output": result.model_dump_json()}


def errand_runner_node(state: FleetState) -> dict[str, Any]:  # noqa: PLR0911, PLR0912
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
        # Resume payload shape (api/approval.py post_approval):
        #   {granted: bool, deferred: bool, approval_token: str, actor: str}
        #
        # A defer is NOT a terminal verdict — per HOMELAB-SPEC Layer 4 the
        # task stays parked at `awaiting_approval` waiting on Rob, it does
        # not auto-execute and it does not complete. So on a defer we loop
        # and `interrupt()` again, re-pausing for a fresh verdict. LangGraph
        # replays the node from the top on each resume and feeds the stored
        # resume values to the interrupt calls in order, so a sequence of
        # defers followed by an approve resolves correctly. The queue worker
        # re-parks with a fresh TTL on each defer (api/approval.py).
        while True:
            verdict: dict[str, Any] = interrupt(req.model_dump())
            if not verdict.get("deferred"):
                break
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
    #
    # The verification is timed for the smoke branch below — when the smoke
    # path runs, the HMAC verify latency is the only measurement that's
    # actually meaningful for catching TS-vs-Python drift (the rest of the
    # smoke is local filesystem). The same `_verify_approval_token` runs in
    # both the smoke and real paths; we time it explicitly so the smoke
    # result envelope can record it.
    signing_secret = settings.langgraph_approval_signing_key or ""
    hmac_verify_start = time.perf_counter()
    hmac_ok = _verify_approval_token(
        approval_token or "",
        task_id=state.task_id,
        action_class=req.action_class,
        server=server,
        method=method,
        signing_secret=signing_secret,
    )
    hmac_verify_ms = (time.perf_counter() - hmac_verify_start) * 1000.0
    if not hmac_ok:
        return {
            "output": (
                f"errand-runner: approval token invalid for "
                f"{state.task_id}|{req.action_class}|{server}|{method}"
            ),
        }

    # Smoke-test branch — intercepts before MCP. The smoke pseudo-server has
    # no MCPGatewayClient route; instead the request is satisfied by an
    # in-process filesystem write+readback+delete cycle that exercises the
    # full approval-flow plumbing (interrupt → ntfy → user → /approval →
    # resume → HMAC verify) without touching any real MCP server. The smoke
    # capability is gated by the same allowlist mechanism as everything
    # else (`smoke.test_write` is in errand-runner's allowlist in mcp.py).
    if server == _SMOKE_SERVER:
        return _run_smoke(
            state,
            hmac_verify_ms=hmac_verify_ms,
            hmac_ok=hmac_ok,
        )

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
