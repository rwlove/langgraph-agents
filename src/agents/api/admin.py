"""Admin / inspection routes for ops + n8n integration.

GET endpoints are read-only inspection. POST endpoints mutate workflow state
(timeout-tier, cancel) and are intended for the n8n awaiting-user-sweep
workflow, not for human use. They should be reachable only inside the
cluster (no public httproute) — defense-in-depth on top of the
NetworkPolicy that constrains ingress.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.paused_threads import (
    DEFAULT_STALE_AFTER_SECONDS,
    sweep_paused_threads,
)
from agents.personas import load_identity
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS, TimeoutTier

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/agents")
async def list_agents() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for agent_id in ALL_AGENT_IDS:
        identity = load_identity(agent_id)
        out.append(
            {
                "id": agent_id,
                "name": identity.name,
                "emoji": identity.emoji,
            }
        )
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
        out.append(
            {
                "task_id": thread_id,
                "target_agent": values.get("target_agent"),
                "awaiting_user_since": values.get("awaiting_user_since"),
                "timeout_tier": values.get("timeout_tier"),
                "interrupts": interrupts,
            }
        )
    return out


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict[str, Any]:
    """Return a task's queue status + checkpointer state.

    Phase 4.M2 — primary client is the daily-digest workflow polling
    for completion after the synchronous /inbox flow was removed.

    Response shape:

    - `task_id`: the queue ULID
    - `queue.status`: pending | claimed | done | (missing if checkpointer-only)
    - `queue.attempts`: dequeue count
    - `queue.result`: { output: str } when status=done
    - `queue.last_error`: only set for failed entries (rare; most failures
      go to task_dlq)
    - `checkpointer.values` / `next` / `interrupts`: same as pre-cutover
    """
    out: dict[str, Any] = {"task_id": task_id}

    pool = request.app.state.queue_pool
    if pool is not None:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, attempts, result, last_error,
                       claimed_at, claimed_by, ttl_expires_at
                FROM task_queue WHERE id = %s
                """,
                (task_id,),
            )
            row = await cur.fetchone()
        if row is not None:
            status, attempts, result, last_error, claimed_at, claimed_by, ttl_expires_at = row
            out["queue"] = {
                "status": status,
                "attempts": attempts,
                "result": result,
                "last_error": last_error,
                "claimed_at": claimed_at.isoformat() if claimed_at else None,
                "claimed_by": claimed_by,
                "ttl_expires_at": ttl_expires_at.isoformat() if ttl_expires_at else None,
            }

    graph = request.app.state.graph
    if graph is not None:
        config = {"configurable": {"thread_id": task_id}}
        try:
            snapshot = await graph.aget_state(config)
            out["checkpointer"] = {
                "values": snapshot.values,
                "next": list(snapshot.next),
                "interrupts": [
                    {"id": i.id, "value": dict(i.value) if i.value else None}
                    for t in snapshot.tasks
                    for i in t.interrupts
                ],
            }
        except Exception:
            # Checkpointer may not have the task if the worker hasn't
            # claimed it yet. Queue side covers the early-life case.
            pass

    if "queue" not in out and "checkpointer" not in out:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")

    return out


class TimeoutTierBody(BaseModel):
    tier: TimeoutTier


@router.post("/tasks/{task_id}/timeout-tier")
async def set_timeout_tier(task_id: str, body: TimeoutTierBody, request: Request) -> dict[str, Any]:
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
async def cancel_task(task_id: str, body: CancelBody, request: Request) -> dict[str, Any]:
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


# --- paused-thread sweep (P3.7) ---


class PausedThreadInterrupt(BaseModel):
    id: str | None = None
    value: dict[str, Any] | None = None


class PausedThread(BaseModel):
    thread_id: str
    paused_since: str | None = None
    paused_for_seconds: float | None = None
    is_stale: bool
    interrupts: list[PausedThreadInterrupt]
    next: list[str]


@router.get("/paused-threads", response_model=list[PausedThread])
async def list_paused_threads(
    request: Request,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> list[dict[str, Any]]:
    """Re-run the paused-thread sweep on demand.

    Same logic that runs on pod startup (see
    ``agents.paused_threads.sweep_paused_threads``). Diagnostic-only —
    no resume / cancel side effects.

    The startup sweep logs each stale thread once per pod lifetime;
    this endpoint lets the operator or a future cost-cap-watcher
    poll for the current inventory without restarting the pod.
    """
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status_code=503, detail="graph not initialized")
    return await sweep_paused_threads(graph, stale_after_seconds=stale_after_seconds)


# --- diagnostic: asyncio task graph ---


@router.get("/asyncio-tasks")
async def dump_asyncio_tasks() -> list[dict[str, Any]]:
    """Dump all asyncio Tasks in the current event loop with their stacks.

    Diagnostic for hung /inbox requests where the parking point isn't
    visible from py-spy thread dumps. py-spy shows the loop is alive but
    can't see asyncio Task awaits; this endpoint walks `asyncio.all_tasks`
    and returns each task's stack so the actual parking await is visible.

    Output per task:
      - name: the asyncio.Task.get_name() value
      - coro: a one-line repr of the wrapped coroutine
      - done: bool
      - cancelled: bool
      - stack: list of "file:lineno function" strings (innermost last)

    Defense-in-depth: this endpoint is inside /admin which the
    cluster CNP gates to network/envoy + collab/open-webui +
    home/windmill-*. Don't expose externally.

    See `project_langgraph_reporter_post_node_hang` for the
    investigation this was added to support.
    """

    def _walk_await_chain(coro: Any) -> list[str]:
        """Walk a coroutine's cr_await chain to enumerate every suspended frame.

        `task.get_stack()` returns only the outer-most suspended frame
        — for our hang investigation we need to see EVERY layer of
        await down to where the actual park is happening. The chain:
        coro.cr_await is the next thing it's awaiting; if that's
        another coroutine we recurse. Generators are similar via
        gi_frame / gi_yieldfrom.

        Output is one "file:lineno function" string per layer,
        outermost first.
        """
        frames: list[str] = []
        seen: set[int] = set()
        cur: Any = coro
        # Hard cap so a self-referential cycle (shouldn't happen but
        # safer to bound) can't lock the endpoint.
        for _ in range(50):
            if cur is None or id(cur) in seen:
                break
            seen.add(id(cur))

            frame = getattr(cur, "cr_frame", None) or getattr(cur, "gi_frame", None)
            if frame is not None:
                frames.append(f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}")

            # cr_await is the next-awaited coroutine on async def;
            # gi_yieldfrom is the equivalent on generators (used by
            # asyncio.gather, asyncio.wait_for, etc.).
            nxt = getattr(cur, "cr_await", None) or getattr(cur, "gi_yieldfrom", None)
            cur = nxt
        return frames

    out: list[dict[str, Any]] = []
    for task in asyncio.all_tasks():
        try:
            coro = task.get_coro()
            coro_repr = repr(coro)[:200]
        except Exception:
            coro = None
            coro_repr = "<repr failed>"

        # task.get_stack() returns only the immediate suspended frame.
        # _walk_await_chain follows cr_await down to the innermost
        # actually-blocked frame — that's where the hang is.
        await_chain: list[str] = []
        if coro is not None:
            try:
                await_chain = _walk_await_chain(coro)
            except Exception as exc:
                await_chain = [f"<walk failed: {type(exc).__name__}>"]

        # Keep task.get_stack() output too as a sanity-check.
        immediate_stack: list[str] = []
        try:
            for frame in task.get_stack():
                immediate_stack.append(
                    f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}"
                )
        except Exception:
            immediate_stack.append("<stack unavailable>")

        out.append(
            {
                "name": task.get_name(),
                "coro": coro_repr,
                "done": task.done(),
                "cancelled": task.cancelled(),
                "await_chain": await_chain,
                "immediate_stack": immediate_stack,
            }
        )
    return out


# ---- DLQ (Phase 4.M3) ----


@router.get("/dlq")
async def list_dlq(
    request: Request,
    *,
    since_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List entries in `task_dlq`, newest first.

    Phase 4.M3 — primary client is the Windmill DLQ-watcher cron which
    posts new entries to Zulip `#dlq`.

    Args:
        since_id: ULID; if set, return only entries with id > since_id.
            The Windmill cron tracks the last-seen ULID in its own
            state so it only surfaces new entries.
        limit: cap on rows returned (default 100). The Windmill cron
            should never need more than its poll-interval-worth of
            entries; the cap is defense-in-depth against an
            unbounded query.
    """
    pool = request.app.state.queue_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="queue pool not initialized")

    where = "WHERE id > %s" if since_id else ""
    params: tuple[Any, ...] = (since_id, limit) if since_id else (limit,)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT id, envelope, attempts, last_error, dlq_at
            FROM task_dlq
            {where}
            ORDER BY id DESC
            LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()

    return [
        {
            "id": row[0],
            "envelope": row[1],
            "attempts": row[2],
            "last_error": row[3],
            "dlq_at": row[4].isoformat() if row[4] else None,
        }
        for row in rows
    ]


@router.delete("/dlq/{task_id}")
async def delete_dlq_entry(task_id: str, request: Request) -> dict[str, Any]:
    """Acknowledge a DLQ entry by deleting it.

    Used by the Windmill DLQ-watcher when the operator reacts to the
    Zulip notification (👍 = ack-and-drop). The task is NOT requeued
    here — that's a separate `/admin/dlq/{task_id}/requeue` if we add
    it later.
    """
    pool = request.app.state.queue_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="queue pool not initialized")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM task_dlq WHERE id = %s RETURNING id",
            (task_id,),
        )
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"dlq entry {task_id!r} not found")
    return {"task_id": task_id, "status": "deleted"}
