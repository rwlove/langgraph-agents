"""Read-only inspection routes for ops + debugging."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from agents.personas import load_identity
from agents.state import ALL_AGENT_IDS

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/agents")
async def list_agents() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for agent_id in ALL_AGENT_IDS:
        identity = load_identity(agent_id)
        out.append({
            "id": agent_id,
            "name": identity.name,
            "emoji": identity.emoji,
        })
    return out


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict[str, Any]:
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

    config = {"configurable": {"thread_id": task_id}}
    snapshot = graph.get_state(config)

    return {
        "task_id": task_id,
        "values": snapshot.values,
        "next": list(snapshot.next),
        "interrupts": [
            {"id": i.id, "value": dict(i.value) if i.value else None}
            for t in snapshot.tasks
            for i in t.interrupts
        ],
    }
