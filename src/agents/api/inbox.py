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
from pydantic import BaseModel

from agents.observability import get_logger
from agents.state import FleetState, Source
from agents.tools.zulip import ZulipNotConfiguredError, send_dm

logger = logging.getLogger("agents.api.inbox")
slog = get_logger("inbox")

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


class InboxResponse(BaseModel):
    task_id: str
    status: str  # "complete" | "paused" | "error"
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
        task_id=req.task_id, source=req.source, user=req.user
    )

    initial_state = FleetState(
        task_id=req.task_id,
        source=req.source,
        content=req.content,
        user=req.user,
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
    if (
        req.source == "zulip"
        and req.zulip_user_id is not None
        and output
    ):
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
            logger.exception(
                "zulip-reply unexpected failure task=%s", req.task_id
            )

    return InboxResponse(
        task_id=req.task_id,
        status="complete",
        output=output,
    )
