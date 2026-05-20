"""POST /inbox — Windmill calls this when a new inbox entry lands.

Runs the full fleet graph. Returns the task_id immediately; the graph runs
async and persists state via the checkpointer. If the graph pauses on an
approval interrupt, the response includes the pause info so Windmill can post
the approval request to Zulip.

When `source="zulip"` and `zulip_user_id` is set, the handler additionally
DMs the graph's final output back to that Zulip user via the triager-bot
identity — the user-visible reply lands in the same DM thread they started.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from opentelemetry import trace
from pydantic import BaseModel

from agents.idempotency import DedupStore
from agents.observability import get_logger
from agents.settings import get_settings
from agents.state import (
    DataTier,
    FleetState,
    Intent,
    Origin,
    Priority,
    Requester,
    RetryPolicy,
    Source,
)
from agents.tools.zulip import ZulipNotConfiguredError, send_dm


def _annotate_current_span(req: InboxRequest) -> None:
    """Phase 3.K — set envelope fields as attributes on the active OTel span.

    No-op when OTel is disabled (`opentelemetry.trace.get_current_span`
    returns a NonRecordingSpan whose `set_attribute` is a noop). Catches
    any exception defensively — observability annotation must never
    break /inbox.
    """
    try:
        span = trace.get_current_span()
        span.set_attribute("app.task_id", req.task_id)
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
            # The envelope `trace_id` is a hint from the caller. The OTel
            # trace_id is the runtime one (extracted from W3C
            # `traceparent` header if present). Recording the envelope
            # hint as an attribute lets ops correlate at search time
            # without losing the runtime trace_id.
            span.set_attribute("app.envelope_trace_id", req.trace_id)
    except Exception:
        # Never let span annotation break /inbox.
        logger.exception("OTel span annotation failed")


logger = logging.getLogger("agents.api.inbox")
slog = get_logger("api.inbox")

router = APIRouter(prefix="", tags=["inbox"])


class InboxRequest(BaseModel):
    task_id: str
    source: Source
    content: str
    user: str = "rob"
    # Optional Zulip routing — when set, the handler DMs the graph's
    # output back to this user_id via the triager-bot identity on
    # graph completion (best-effort; failures logged not raised).
    # The Windmill `zulip-triager-webhook` script passes
    # `message.sender_id` here.
    zulip_user_id: int | None = None

    # --- Task envelope (HOMELAB-SPEC Layer 5; additive, all optional) -------
    #
    # These fields are accepted but not yet acted on. Phase 3 wires them:
    # - `idempotency_key` → dedup at /inbox (3.G)
    # - `data_tier` → redaction layer before vault/Claude (3.H)
    # - `requester` → Renee allowlist enforcement (3.I)
    # - `trace_id` → OTel propagation across mode hops (3.K)
    # Old callers (Windmill triager, holmesgpt webhook, daily-digest cron)
    # send none of these; defaults preserve current behavior.
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


class InboxResponse(BaseModel):
    task_id: str
    status: str  # "complete" | "paused" | "error" | "duplicate"
    output: str | None = None
    paused_for: dict[str, Any] | None = None


@router.post("/inbox", response_model=InboxResponse)
async def post_inbox(req: InboxRequest, request: Request) -> InboxResponse:
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

    # Bind task_id + source on structlog contextvars so every structlog
    # event emitted while serving this request carries them. The per-node
    # wrapper in graphs/fleet.py additionally binds `agent` per-call, giving
    # Loki the {agent, task_id, event} triple needed to render the dashboard
    # task-trail viewer. Bindings live in the asyncio task's contextvars and
    # die with the task — no manual unbind needed in the FastAPI request path.
    structlog.contextvars.bind_contextvars(
        task_id=req.task_id,
        source=req.source,
        user=req.user,
        # Phase 3.H — data_tier on the asyncio task's contextvars so
        # downstream LLM calls (agents.llm._build_claude) can read it
        # without threading the value through every function signature.
        data_tier=req.data_tier,
    )

    # Phase 3.K — annotate the FastAPI-instrumented span with envelope
    # fields. No-op when OTel is disabled / collector unreachable.
    _annotate_current_span(req)

    # Phase 3.I — Renee allowlist (HOMELAB-SPEC Layer 7).
    # When `requester="renee"`, the envelope's `intent` must be in the
    # operator-configured allowlist. Other requesters (rob, system,
    # None) skip the check. Default scope is the "medium" decision
    # from 2026-05-20 (action + question).
    if req.requester == "renee":
        settings = get_settings()
        if req.intent is None:
            slog.warning(
                "inbox_renee_intent_missing",
                idempotency_key=req.idempotency_key,
            )
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

    # Phase 3.G — idempotency-key dedup. If the caller passed an
    # `idempotency_key` and we've seen it within the TTL window, return
    # the cached response (the prior task_id) instead of re-running the
    # graph. Old callers don't pass the key → no dedup, no behavior
    # change. DedupStore degrades gracefully on Dragonfly outage (logs
    # warning, returns None → falls through to run the graph).
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
                status="duplicate",
            )

    initial_state = FleetState(
        task_id=req.task_id,
        source=req.source,
        content=req.content,
        user=req.user,
        # Envelope fields propagate from InboxRequest. Defaults match.
        trace_id=req.trace_id,
        origin=req.origin,
        requester=req.requester,
        intent_envelope=req.intent,
        priority=req.priority,
        destructive=req.destructive,
        idempotency_key=req.idempotency_key,
        ttl_seconds=req.ttl_seconds,
        retry_policy=req.retry_policy,
        data_tier=req.data_tier,
    )

    config = {"configurable": {"thread_id": req.task_id}}

    slog.info("inbox_start", content_preview=req.content[:120])

    # Run the graph. If it interrupts, the returned state has the interrupt
    # payload accessible via the graph's `aget_state(config).next` and tasks
    # info; for phase 1 we surface state.output directly.
    final = await graph.ainvoke(initial_state, config=config)

    # Detect pause: LangGraph returns the partial state at interrupt point.
    # MUST use aget_state (async) — the checkpointer is AsyncPostgresSaver,
    # and sync get_state from the main async event loop raises
    # asyncio.InvalidStateError.
    state_snapshot = await graph.aget_state(config)
    interrupts = state_snapshot.tasks[0].interrupts if state_snapshot.tasks else ()

    if interrupts:
        # On pause, we let Windmill's approval-post flow handle the user-
        # visible message — don't DM here. The approval-post topic carries
        # the full request and the action buttons.
        slog.info("inbox_paused", interrupt_count=len(interrupts))
        return InboxResponse(
            task_id=req.task_id,
            status="paused",
            paused_for=dict(interrupts[0].value) if interrupts[0].value else None,
        )

    output = final.get("output")
    slog.info(
        "inbox_complete",
        has_output=output is not None,
        output_len=len(output) if output else 0,
    )

    # Zulip reply-back path: only fires when the caller is the triager-bot
    # outgoing-webhook and the request carried a user_id to reply to.
    if req.source == "zulip" and req.zulip_user_id is not None and output:
        try:
            result = send_dm(req.zulip_user_id, output)
            logger.info(
                "zulip-reply task=%s user_id=%s status=%s msg_id=%s",
                req.task_id,
                req.zulip_user_id,
                result.status_code,
                result.msg_id,
            )
        except ZulipNotConfiguredError as exc:
            logger.warning(
                "zulip-reply skipped (config missing) task=%s: %s",
                req.task_id,
                exc,
            )
        except Exception:
            # Best-effort post-back: never fail the request because the
            # secondary Zulip POST broke.
            logger.exception("zulip-reply unexpected failure task=%s", req.task_id)

    return InboxResponse(
        task_id=req.task_id,
        status="complete",
        output=output,
    )
