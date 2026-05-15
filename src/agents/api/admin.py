"""Admin / inspection routes for ops + n8n integration.

GET endpoints are read-only inspection. POST endpoints mutate workflow state
(timeout-tier, cancel) and are intended for the n8n awaiting-user-sweep
workflow, not for human use. They should be reachable only inside the
cluster (no public httproute) — defense-in-depth on top of the
NetworkPolicy that constrains ingress.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.personas import load_identity
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS, TimeoutTier

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


@router.get("/tasks")
async def list_tasks(request: Request) -> list[dict[str, Any]]:
    """List all tasks the checkpointer knows about.

    Used by n8n's awaiting-user-sweep to find paused workflows. The
    checkpointer's list/aget_state API is the source of truth.
    """
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

    out: list[dict[str, Any]] = []
    # graph.checkpointer.alist({}) yields ALL checkpoints across threads.
    # We dedupe to the latest per thread.
    seen: set[str] = set()
    async for cp in graph.checkpointer.alist({}):
        thread_id = cp.config.get("configurable", {}).get("thread_id")
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        interrupts = [
            {"id": i.id, "value": dict(i.value) if i.value else None}
            for t in snapshot.tasks
            for i in t.interrupts
        ]
        values = snapshot.values or {}
        out.append({
            "task_id": thread_id,
            "target_agent": values.get("target_agent"),
            "awaiting_user_since": values.get("awaiting_user_since"),
            "timeout_tier": values.get("timeout_tier"),
            "interrupts": interrupts,
        })
    return out


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict[str, Any]:
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

    config = {"configurable": {"thread_id": task_id}}
    snapshot = await graph.aget_state(config)

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


class TimeoutTierBody(BaseModel):
    tier: TimeoutTier


@router.post("/tasks/{task_id}/timeout-tier")
async def set_timeout_tier(
    task_id: str, body: TimeoutTierBody, request: Request
) -> dict[str, Any]:
    """Mark a paused workflow as cold (4h) or whatever tier n8n decides.

    The supervisor's per-agent override logic lives at the node level;
    this endpoint just persists the timeout-tier state. The state.py
    `timeout_tier` field is a Literal so invalid values are rejected by
    Pydantic at parse time.
    """
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

    config = {"configurable": {"thread_id": task_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    await graph.aupdate_state(
        config,
        {"timeout_tier": body.tier},
    )
    return {"task_id": task_id, "timeout_tier": body.tier, "status": "updated"}


class CancelBody(BaseModel):
    reason: str = "auto-cancelled"


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, body: CancelBody, request: Request
) -> dict[str, Any]:
    """Cancel a paused workflow.

    Sets a sentinel output field that any downstream reader interprets as
    "task ended without completion". LangGraph doesn't have a native "kill
    a thread" call; this is the agreed-upon convention.
    """
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")

    config = {"configurable": {"thread_id": task_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")

    await graph.aupdate_state(
        config,
        {
            "output": f"CANCELLED: {body.reason}",
            "approval_granted": False,
        },
    )
    return {"task_id": task_id, "status": "cancelled", "reason": body.reason}


# ---- cost cap ----


class CostSummary(BaseModel):
    spent_usd: float
    daily_cap_usd: float
    per_agent: dict[str, float]
    date: date


@router.get("/costs/today", response_model=CostSummary)
async def costs_today() -> CostSummary:
    """Best-effort spend summary for today.

    Phase 5+ deliverable: when Langfuse is deployed, source the per-task
    cost from Langfuse's API. Until then, return zeros from a local
    JSONL log + the configured caps. The n8n cost-cap-watcher reads this
    endpoint and never errors on missing data.
    """
    settings = get_settings()
    today = datetime.now(UTC).date()

    # Phase-5 stub: read from a local activity log if it exists; otherwise
    # zeros. The actual Langfuse-backed implementation lands later.
    cost_log = settings.vault_root / "reports" / "costs" / f"{today.isoformat()}.jsonl"
    spent = 0.0
    per_agent: dict[str, float] = {}
    if cost_log.is_file():
        try:
            for line in cost_log.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line) if line.strip() else {}
                usd = float(entry.get("cost_usd", 0.0))
                spent += usd
                agent = entry.get("agent")
                if agent:
                    per_agent[agent] = per_agent.get(agent, 0.0) + usd
        except (OSError, ValueError):
            pass  # graceful degradation

    return CostSummary(
        spent_usd=round(spent, 4),
        daily_cap_usd=settings.cost_cap_global_daily_usd,
        per_agent=per_agent,
        date=today,
    )
