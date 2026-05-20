"""POST /approval — resume a paused workflow on a user verdict.

Approval verdicts arrive from the ntfy action buttons on the user's phone
(tap-to-approve, signed with an HMAC token). The button POSTs here over
cloudflared, the token is verified by errand-runner downstream, and the
LangGraph workflow resumes via ``Command(resume=...)``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel

router = APIRouter(prefix="", tags=["approval"])

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
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

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

    return ApprovalResponse(
        task_id=req.task_id,
        status="complete",
        output=final.get("output"),
    )
