"""POST /approval — resume a paused workflow on a user verdict.

Approval verdicts arrive from the ntfy action buttons on the user's phone
(tap-to-approve, signed with an HMAC token). The button POSTs here over
cloudflared, the token is verified by errand-runner downstream, and the
LangGraph workflow resumes via ``Command(resume=...)``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel

from agents.observability import get_logger
from agents.settings import get_settings

router = APIRouter(prefix="", tags=["approval"])
slog = get_logger("api.approval")

Reaction = Literal["approve", "reject", "defer"]


class ApprovalRequest(BaseModel):
    task_id: str
    reaction: Reaction
    approval_token: str  # HMAC-signed; verified by errand-runner downstream
    actor: str = "rob"


class ApprovalResponse(BaseModel):
    task_id: str
    status: str  # "resumed" | "complete" | "error"
    output: str | None = None


@router.post("/approval", response_model=ApprovalResponse)
async def post_approval(req: ApprovalRequest, request: Request) -> ApprovalResponse:
    # Pick the graph that originally invoked the interrupt. Smoke runs use
    # the single-node `smoke_graph` (START → errand-runner → END); production
    # tasks use the full `fleet_graph` (which ends with reporter). Resuming
    # on the wrong graph means the reporter renders the smoke's structured
    # JSON output into prose (lost timings) OR a fleet task tries to run
    # on the smoke graph and hits an unregistered-node error mid-resume.
    #
    # Discriminator: smoke task_ids are minted by /admin/smoke/start-approval
    # with the prefix `smoke-`. Anything else is a production task.
    if req.task_id.startswith("smoke-"):
        graph = getattr(request.app.state, "smoke_graph", None)
        graph_name = "smoke_graph"
    else:
        graph = request.app.state.graph
        graph_name = "fleet_graph"

    if graph is None:
        raise HTTPException(
            status_code=503,
            detail=f"{graph_name} not initialized",
        )

    # Bind contextvars so every structlog event during the resume carries
    # the task_id + actor + reaction. Same async-task-scoped binding pattern
    # used in `api/inbox.py`; lives for the duration of this request handler.
    structlog.contextvars.bind_contextvars(
        task_id=req.task_id,
        actor=req.actor,
        reaction=req.reaction,
        graph=graph_name,
    )
    slog.info("approval_received")

    config = {"configurable": {"thread_id": req.task_id}}

    # Verify the workflow is actually paused at an interrupt. MUST use
    # `aget_state` (async) — the checkpointer is `AsyncPostgresSaver`, and
    # sync `get_state` from the main async event loop raises
    # `asyncio.InvalidStateError`. Same constraint applies in `api/inbox.py`.
    snapshot = await graph.aget_state(config)
    if not snapshot.tasks or not snapshot.tasks[0].interrupts:
        raise HTTPException(
            status_code=409,
            detail=f"task {req.task_id} is not paused at an approval interrupt",
        )

    resume_value = {
        "granted": req.reaction == "approve",
        "deferred": req.reaction == "defer",
        "approval_token": req.approval_token,
        "actor": req.actor,
    }

    final = await graph.ainvoke(Command(resume=resume_value), config=config)

    output = final.get("output")

    # Reconcile the durable queue row with what the graph just did. The
    # worker parked this task at `awaiting_approval` (queue/worker.py) when
    # it first hit the interrupt; the queue still believes it's parked.
    # Branch on the POST-resume graph state, not on `req.reaction`: a defer
    # re-interrupts (graphs/approval.py), so the graph is paused again and
    # we re-park with a fresh deadline; approve/reject run to a terminal
    # state, so we ack `done`.
    #
    # Smoke tasks (`smoke-` prefix) never entered the durable queue, and a
    # queue-disabled deployment has `task_queue is None` — both skip
    # reconciliation. `resolve_approval` is itself no-op-safe (it only acts
    # on rows still `awaiting_approval`), so a row the guardian sweep already
    # expired to the DLQ can't be resurrected here.
    queue = getattr(request.app.state, "task_queue", None)
    if queue is not None and not req.task_id.startswith("smoke-"):
        post_resume = await graph.aget_state(config)
        still_paused = bool(post_resume.tasks and post_resume.tasks[0].interrupts)
        if still_paused:
            deadline = datetime.now(UTC) + timedelta(
                seconds=get_settings().approval_ttl_seconds
            )
            await queue.park_for_approval(req.task_id, deadline)
            slog.info("approval_resumed_reparked")
            return ApprovalResponse(
                task_id=req.task_id,
                status="resumed",
                output=output,
            )
        await queue.resolve_approval(req.task_id, output)

    slog.info("approval_resumed_complete", has_output=output is not None)

    return ApprovalResponse(
        task_id=req.task_id,
        status="complete",
        output=output,
    )
