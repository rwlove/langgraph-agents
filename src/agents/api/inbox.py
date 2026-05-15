"""POST /inbox — n8n calls this when a new inbox entry lands.

Runs the full fleet graph. Returns the task_id immediately; the graph runs
async and persists state via the checkpointer. If the graph pauses on an
approval interrupt, the response includes the pause info so n8n can post
the approval request to Zulip.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.state import FleetState, Source

router = APIRouter(prefix="", tags=["inbox"])


class InboxRequest(BaseModel):
    task_id: str
    source: Source
    content: str
    user: str = "rob"


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

    initial_state = FleetState(
        task_id=req.task_id,
        source=req.source,
        content=req.content,
        user=req.user,
    )

    config = {"configurable": {"thread_id": req.task_id}}

    # Run the graph. If it interrupts, the returned state has the interrupt
    # payload accessible via the graph's `get_state(config).next` and tasks
    # info; for phase 1 we surface state.output directly.
    final = await graph.ainvoke(initial_state, config=config)

    # Detect pause: LangGraph returns the partial state at interrupt point.
    state_snapshot = graph.get_state(config)
    interrupts = state_snapshot.tasks[0].interrupts if state_snapshot.tasks else ()

    if interrupts:
        return InboxResponse(
            task_id=req.task_id,
            status="paused",
            paused_for=dict(interrupts[0].value) if interrupts[0].value else None,
        )

    return InboxResponse(
        task_id=req.task_id,
        status="complete",
        output=final.get("output"),
    )
