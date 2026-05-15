"""POST /approval — n8n's approval-broker resumes a paused workflow.

When the user reacts (👍 / 👎 / ⏸️) in Zulip, n8n verifies the reaction is
on the correct message + signed-token matches, then calls this endpoint with
the resolution. We resume the LangGraph workflow with that resolution.
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
    approval_token: str  # n8n-signed HMAC; verified by errand-runner downstream
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

    # Verify the workflow is actually paused at an interrupt
    snapshot = graph.get_state(config)
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
