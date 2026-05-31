"""POST /inbox — accepts a task envelope and enqueues for async processing.

Phase 4.M2 cutover: the synchronous-invoke flow is gone. /inbox now
returns 202 with the assigned task_id; the worker (`agents.queue.worker`)
drains the queue, runs the graph, and writes the result to `task_queue.result`
for poll retrieval via `/admin/tasks/<id>`.

Behavior preserved from synchronous /inbox:

- Idempotency dedup (3.G) at the Dragonfly fast-path — duplicate
  requests return the prior task_id with status="duplicate".
- Renee allowlist (3.I) — `requester="renee"` requires `intent` in
  the allowlist; 403 if not.
- Zulip DM-back continues working — the worker calls `send_dm` after
  graph completion when `source="zulip"` + `zulip_user_id` is set.

Callers that previously relied on synchronous `status="complete"` +
`output` in the response should poll `/admin/tasks/<id>`. See the
companion home-ops PR for the updated daily-digest workflow.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from opentelemetry import trace
from pydantic import BaseModel

from agents.idempotency import DedupStore
from agents.observability import (
    get_logger,
    langgraph_inbox_envelope_rejected_total,
)
from agents.router import estimate_input_tokens
from agents.settings import get_settings
from agents.state import (
    ALL_AGENT_IDS,
    DataTier,
    Intent,
    Origin,
    Priority,
    Requester,
    RetryPolicy,
    Source,
)

logger = logging.getLogger("agents.api.inbox")
slog = get_logger("api.inbox")

router = APIRouter(prefix="", tags=["inbox"])


class InboxRequest(BaseModel):
    # `task_id` is now advisory — the queue assigns a ULID on enqueue.
    # Kept on the request shape for back-compat (callers send it; we
    # log it but the canonical id comes from the queue).
    task_id: str
    source: Source
    content: str
    user: str = "rob"
    zulip_user_id: int | None = None

    # --- Task envelope (HOMELAB-SPEC Layer 5; additive, all optional) -------
    trace_id: str | None = None
    origin: Origin | None = None
    requester: Requester | None = None
    intent: Intent | None = None
    priority: Priority = "normal"
    destructive: bool | None = None
    idempotency_key: str | None = None
    ttl_seconds: int | None = None
    retry_policy: RetryPolicy | None = None
    data_tier: DataTier = "internal"

    # When set, the supervisor/triager loop respects this pin and skips
    # its own routing decision. Used by Windmill workflows that already
    # know the right specialist (e.g., alertmanager-holmesgpt-notify
    # routes by namespace; langgraph-renovate-triage knows it's a code
    # task). Bypasses qwen2.5:7b's known mis-routing of triage-style
    # prompts to errand-runner. None = let triager decide as before.
    target_agent: str | None = None

    # When set, the worker uses this as the LangGraph thread_id instead
    # of the queue-assigned task_id. Pass the `conversation_id` from a
    # prior response to continue that thread (multi-turn conversation).
    # Absent → new thread, new task_id becomes the conversation_id.
    conversation_id: str | None = None


class InboxResponse(BaseModel):
    task_id: str
    status: str  # "accepted" | "duplicate"
    # The LangGraph thread_id used for this task. Pass as `conversation_id`
    # on the next request to continue the same conversation thread.
    conversation_id: str
    # Legacy callers may still read these; both are absent post-cutover.
    # Poll `/admin/tasks/<id>` for the final output.
    output: str | None = None
    paused_for: dict[str, Any] | None = None


def _validate_envelope(req: InboxRequest) -> None:
    """Reject malformed task envelopes at ingress (HOMELAB-SPEC Layer 5).

    Lenient / minimal-mandatory: this enforces only the semantic checks
    Pydantic can't express on its own. Type-level invalids are already
    422'd at parse time — `source` / `priority` / `data_tier` / `requester`
    are `Literal`s, and `RetryPolicy` carries `Field(ge=...)` bounds
    (`max_attempts >= 1`, `backoff_seconds >= 0`). The optional envelope
    fields (`origin`, `requester`, `data_tier`, `priority`, `destructive`,
    `ttl_seconds`, `retry_policy`) intentionally ride on their defaults —
    no live caller populates the full envelope, so requiring them would
    reject every current request. The gate tightens naturally as callers
    enrich.

    Raises HTTPException(422) with a stable `reason` slug on the first
    failing check; each rejection also increments
    ``langgraph_inbox_envelope_rejected_total{reason}``.
    """

    def _reject(reason: str, detail: str) -> None:
        langgraph_inbox_envelope_rejected_total.labels(reason=reason).inc()
        slog.warning("inbox_envelope_rejected", reason=reason, client_task_id=req.task_id)
        raise HTTPException(status_code=422, detail=detail)

    if req.task_id.strip() == "":
        _reject("task_id_blank", "`task_id` must be a non-empty string")
    if req.content.strip() == "":
        _reject("content_blank", "`content` must be a non-empty string")
    if req.ttl_seconds is not None and req.ttl_seconds <= 0:
        _reject("ttl_nonpositive", "`ttl_seconds`, when set, must be > 0")
    if req.idempotency_key is not None and req.idempotency_key.strip() == "":
        _reject("idempotency_key_blank", "`idempotency_key`, when set, must be non-empty")
    if req.target_agent is not None and req.target_agent not in ALL_AGENT_IDS:
        _reject(
            "unknown_target_agent",
            f"`target_agent`={req.target_agent!r} is not a known agent id",
        )


def _ensure_trace_id(req: InboxRequest) -> None:
    """Mint a trace_id at ingress when the envelope omits one.

    HOMELAB-SPEC Layer 5: every task carries a `trace_id` that follows
    it from ingress through every mode hop. Most callers (Open WebUI,
    HA voice, Android, Windmill) don't supply one, so we mint it here
    — the single ingress point — giving the task a stable id for its
    whole life in the queue, worker, graph, and webhook payloads.

    Prefer the active OTel span's trace_id so structured logs and the
    OTel trace share one id; fall back to a fresh random 128-bit id
    when no span is active (tests, non-instrumented call paths).
    """
    if req.trace_id is not None:
        return
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        req.trace_id = format(span_context.trace_id, "032x")
    else:
        req.trace_id = secrets.token_hex(16)


def _annotate_current_span(req: InboxRequest, queue_task_id: str) -> None:
    """Phase 3.K — set envelope fields as attributes on the active OTel span."""
    try:
        span = trace.get_current_span()
        span.set_attribute("app.task_id", queue_task_id)
        span.set_attribute("app.client_task_id", req.task_id)
        span.set_attribute("app.source", req.source)
        span.set_attribute("app.user", req.user)
        span.set_attribute("app.data_tier", req.data_tier)
        span.set_attribute("app.priority", req.priority)
        if req.origin is not None:
            span.set_attribute("app.origin", req.origin)
        if req.requester is not None:
            span.set_attribute("app.requester", req.requester)
        if req.intent is not None:
            span.set_attribute("app.intent", req.intent)
        if req.destructive is not None:
            span.set_attribute("app.destructive", req.destructive)
        if req.idempotency_key is not None:
            span.set_attribute("app.idempotency_key", req.idempotency_key)
        if req.trace_id is not None:
            span.set_attribute("app.envelope_trace_id", req.trace_id)
    except Exception:
        logger.exception("OTel span annotation failed")


@router.post("/inbox", response_model=InboxResponse, status_code=202)
async def post_inbox(req: InboxRequest, request: Request) -> InboxResponse:
    """Accept a task envelope and return 202 + task_id.

    The worker drains the queue in the background. Poll `/admin/tasks/<id>`
    for status + final output. Synchronous response semantics
    (`status="complete"` + `output`) are gone — see the module docstring.
    """
    queue = request.app.state.task_queue
    if queue is None:
        raise HTTPException(status_code=503, detail="task queue not initialized")

    # Task-contract validation (HOMELAB-SPEC Layer 5) — reject malformed
    # envelopes before any dedup-store side effect or enqueue.
    _validate_envelope(req)

    # Renee allowlist (3.I) — same gate as the synchronous flow.
    if req.requester == "renee":
        settings = get_settings()
        if req.intent is None:
            slog.warning("inbox_renee_intent_missing", client_task_id=req.task_id)
            raise HTTPException(
                status_code=422,
                detail="requester=renee requires `intent` field on the envelope",
            )
        if req.intent not in settings.renee_allowed_intents:
            slog.warning(
                "inbox_renee_intent_blocked",
                intent=req.intent,
                allowed=sorted(settings.renee_allowed_intents),
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"intent={req.intent!r} is not in Renee's allowlist; "
                    "this request would route to Rob's approval queue if the "
                    "substrate were available — for now it's rejected."
                ),
            )

    # Idempotency-key fast-path (3.G) — Dragonfly cache returns prior
    # task_id if seen within the TTL window. Postgres' unique partial
    # index is the defense-in-depth (UniqueViolation on enqueue).
    if req.idempotency_key is not None:
        dedup_store: DedupStore = request.app.state.dedup_store
        prior_task_id = await dedup_store.check_and_set(
            key=req.idempotency_key,
            value=req.task_id,
        )
        if prior_task_id is not None and prior_task_id != "":
            slog.info(
                "inbox_idempotency_hit",
                idempotency_key=req.idempotency_key,
                returned_task_id=prior_task_id,
            )
            return InboxResponse(
                task_id=prior_task_id,
                conversation_id=prior_task_id,
                status="duplicate",
            )

    _ensure_trace_id(req)
    envelope: dict[str, Any] = req.model_dump(mode="json")
    queue_task_id = await queue.enqueue(envelope)

    structlog.contextvars.bind_contextvars(
        task_id=queue_task_id,
        trace_id=req.trace_id,
        source=req.source,
        user=req.user,
        data_tier=req.data_tier,
        est_input_tokens=estimate_input_tokens(req.content),
        destructive=bool(req.destructive),
    )
    _annotate_current_span(req, queue_task_id)

    # The LangGraph thread_id is either the caller-supplied conversation_id
    # (thread continuation) or the queue-assigned task_id (new thread).
    conversation_id = req.conversation_id or queue_task_id

    slog.info(
        "inbox_enqueued",
        client_task_id=req.task_id,
        queue_task_id=queue_task_id,
        conversation_id=conversation_id,
        content_preview=req.content[:120],
    )
    return InboxResponse(task_id=queue_task_id, conversation_id=conversation_id, status="accepted")
