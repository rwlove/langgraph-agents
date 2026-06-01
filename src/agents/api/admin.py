"""Admin / inspection routes for ops + Windmill integration.

GET endpoints are read-only inspection. POST endpoints mutate workflow state
(timeout-tier, cancel) and are intended for the Windmill awaiting-user-sweep
workflow, not for human use. They should be reachable only inside the
cluster (no public httproute) — defense-in-depth on top of the
NetworkPolicy that constrains ingress.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel, Field

from agents.llm import AGENT_GROUP
from agents.observability import get_logger
from agents.paused_threads import (
    DEFAULT_STALE_AFTER_SECONDS,
    sweep_paused_threads,
)
from agents.personas import load_identity
from agents.queue.approval_post import post_approval_for_interrupts
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS, ApprovalRequest, FleetState, TimeoutTier

router = APIRouter(prefix="/admin", tags=["admin"])
slog = get_logger("api.admin")


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


async def _list_tasks_by_status(request: Request, status: str) -> list[dict[str, Any]]:
    """Cheap status-filtered listing straight from `task_queue`.

    No checkpointer scan — a single indexed SELECT. Newest-first by ULID.
    Returns 503 if the queue substrate isn't wired (pool is None).
    """
    pool = request.app.state.queue_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="queue substrate not initialized")

    out: list[dict[str, Any]] = []
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, envelope, status, approval_expires_at, updated_at,
                   approval_request
            FROM task_queue
            WHERE status = %s
            ORDER BY id DESC
            """,
            (status,),
        )
        async for row in cur:
            envelope = row[1] or {}
            approval_expires_at = row[3]
            updated_at = row[4]
            out.append(
                {
                    "task_id": row[0],
                    "queue_status": row[2],
                    "content": envelope.get("content") or "",
                    "source": envelope.get("source") or "",
                    "user": envelope.get("user") or "",
                    "approval_expires_at": (
                        approval_expires_at.isoformat() if approval_expires_at else None
                    ),
                    "updated_at": updated_at.isoformat() if updated_at else None,
                    # Curated ApprovalRequest subset persisted at park time
                    # (approval_post.curate_approval_request): payload_summary,
                    # action_class, proposed_by, undo_path, cost_estimate_usd,
                    # requires_two_person. NULL for non-approval statuses and
                    # for rows parked before this column existed. The HA
                    # Companion card renders payload_summary as the headline.
                    "approval_request": row[5],
                }
            )
    return out


@router.get("/tasks")
async def list_tasks(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    """List all tasks the checkpointer knows about.

    Used by the awaiting-user-sweep Windmill script to find paused
    workflows. Reads run through `app.state.admin_graph`, which has
    its own checkpointer instance (own pool, own `asyncio.Lock`) so
    the dispatch path's hung calls can't block us. See `main.py`
    lifespan comment for that isolation.

    `?status=<queue-status>` short-circuits to a cheap, status-indexed
    `task_queue` query and skips the checkpointer scan entirely. This
    is the guardian's read surface — `?status=awaiting_approval` lists
    only the tasks parked for Rob's verdict (backed by the
    `idx_task_queue_awaiting` partial index), carrying their
    `approval_expires_at` so the operator can see which are closest to
    lapsing. The unfiltered call keeps the full checkpointer-backed
    listing below.

    Implementation is two-phase to avoid SELF-deadlocking on the
    admin saver's own lock: `AsyncPostgresSaver._cursor` does
    `async with self.lock, get_connection(...)` and holds that lock
    across `alist`'s `yield` points (langgraph
    `checkpoint/postgres/aio.py`). The non-re-entrant `asyncio.Lock`
    means an `aget_state` call inside the `alist` loop body tries to
    re-acquire a lock the same task already holds — and waits forever.
    Drain `alist` to `thread_id`s first (lock released when iterator
    exits), then call `aget_state` per thread (each one takes and
    releases the lock cleanly).
    """
    if status is not None:
        return await _list_tasks_by_status(request, status)

    graph = request.app.state.admin_graph
    if graph is None:
        raise HTTPException(status_code=503, detail="admin graph not initialized")

    # Phase 1 — drain alist into unique thread_ids. Saver lock is
    # released the moment this iterator exits.
    seen: set[str] = set()
    async for cp in graph.checkpointer.alist({}):
        thread_id = cp.config.get("configurable", {}).get("thread_id")
        if thread_id:
            seen.add(thread_id)

    # Pre-fetch envelope + queue status from task_queue for every known
    # thread so the listing carries the originating prompt content
    # (Stage 2 dogfooding fix — `task_id` alone is unmappable to "what
    # I asked"). Single SELECT for the whole set rather than N
    # round-trips inside the per-thread loop below.
    envelope_by_id: dict[str, dict[str, Any]] = {}
    queue_status_by_id: dict[str, str] = {}
    pool = request.app.state.queue_pool
    if pool is not None and seen:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT id, envelope, status FROM task_queue WHERE id = ANY(%s)",
                (list(seen),),
            )
            async for row in cur:
                envelope_by_id[row[0]] = row[1] or {}
                queue_status_by_id[row[0]] = row[2]

    # Phase 2 — aget_state per thread. Each call acquires + releases
    # the saver lock independently, so no self-deadlock. Sequential
    # rather than gathered because every call serializes through the
    # same lock anyway; gather would only add complexity.
    out: list[dict[str, Any]] = []
    for thread_id in seen:
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        interrupts = [
            {"id": i.id, "value": dict(i.value) if i.value else None}
            for t in snapshot.tasks
            for i in t.interrupts
        ]
        values = snapshot.values or {}
        envelope = envelope_by_id.get(thread_id, {})
        out.append(
            {
                "task_id": thread_id,
                "target_agent": values.get("target_agent"),
                "awaiting_user_since": values.get("awaiting_user_since"),
                "timeout_tier": values.get("timeout_tier"),
                "interrupts": interrupts,
                # Originating prompt context — sourced from the queue
                # envelope. Empty strings when the task pre-dates the
                # queue substrate (Phase <4.M1 checkpointer-only tasks).
                "content": envelope.get("content") or "",
                "source": envelope.get("source") or "",
                "user": envelope.get("user") or "",
                "queue_status": queue_status_by_id.get(thread_id, ""),
            }
        )
    # Stable order: newest-claimed first (paused-for-user surface) by
    # falling back on task_id ULID's lexicographic ordering, which is
    # monotonic. Lets the CLI render a recent-first table without a
    # separate sort key on the wire.
    out.sort(key=lambda t: str(t["task_id"]), reverse=True)
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

    # Read-only checkpointer access — use the admin graph so the
    # daily-digest polling loop can't be blocked by a hung dispatch-path
    # call on the main checkpointer's lock. See `main.py` lifespan.
    graph = request.app.state.admin_graph
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
    """Mark a paused workflow as cold (4h) or whatever tier Windmill decides.

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


# ---- approve / deny (HA Companion tap-to-act surface) ----
#
# These two endpoints are the server-side half of the HA-Companion
# approvals flow (HomeAIOps Gate-2 slice 3). They do for the phone what
# the ntfy action-buttons do via Windmill: resume a paused approval
# interrupt with a verdict. The difference is WHERE the HMAC token comes
# from. The ntfy buttons carry a token Windmill minted; these endpoints
# mint it HERE, server-side, so the phone never has to hold the signing
# secret — HA just POSTs its existing `/admin` bearer (the same
# `hai_cli_token` it already uses for the slice-1 voice→task flow) and
# names the task. The minted token still has to satisfy errand-runner's
# downstream `_verify_approval_token`, so the signing format below is a
# hard mirror of that verifier; `tests/test_admin_approve_deny.py`
# round-trips against the real verifier to catch any drift.
#
# Auth: `/admin/*` is Bearer-gated by `api.auth.hai_cli_auth_middleware`,
# so these inherit the same gate as every other admin mutation. The
# `/approval` endpoint (ntfy path) is deliberately auth-exempt because
# its body carries the HMAC token AS the auth; here the bearer is the
# auth and the token is minted under it.


def _sign_approval_token(
    task_id: str,
    action_class: str,
    server: str,
    method: str,
    *,
    signing_secret: str,
    nonce: str | None = None,
) -> str:
    """Mint an HMAC-SHA256 approval token errand-runner will accept.

    Format is the exact contract `errand_runner._verify_approval_token`
    checks: ``task_id|class|server|method|<nonce>:<hex-sig>`` where the
    signature is ``HMAC-SHA256(secret, "task_id|class|server|method|nonce")``.
    Same scheme Windmill's `langgraph-approval-post.ts` uses — this is the
    in-cluster equivalent so the phone path needs no Windmill round-trip.
    The nonce is random per mint (errand-runner ignores its content; it
    only requires the first four fields to match the live request).
    """
    nonce = nonce or secrets.token_hex(8)
    payload = f"{task_id}|{action_class}|{server}|{method}|{nonce}"
    signature = hmac.new(
        signing_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def _pick_graph(request: Request, task_id: str) -> tuple[Any, str]:
    """Resolve smoke vs fleet graph by task_id prefix (mirrors api/approval)."""
    if task_id.startswith("smoke-"):
        return getattr(request.app.state, "smoke_graph", None), "smoke_graph"
    return request.app.state.graph, "fleet_graph"


async def _resume_with_verdict(
    request: Request,
    task_id: str,
    *,
    granted: bool,
    actor: str,
) -> dict[str, Any]:
    """Resume a paused approval interrupt with a verdict; reconcile the queue.

    Shared body for the approve/deny endpoints. On approve, mints a fresh
    signed token from the live interrupt's `action_class` + `target` so
    errand-runner's HMAC verify passes. On deny, no valid token is needed
    (errand-runner refuses before it verifies), so an empty token is sent.

    Reconciliation mirrors `api/approval.post_approval`: if the graph is
    STILL paused after the resume (a defer, or the two-person second
    confirmation re-interrupt), re-park with a fresh TTL and report
    `resumed`; otherwise the graph ran to a terminal state and we ack the
    queue row `done`.
    """
    graph, graph_name = _pick_graph(request, task_id)
    if graph is None:
        raise HTTPException(status_code=503, detail=f"{graph_name} not initialized")

    structlog.contextvars.bind_contextvars(
        task_id=task_id,
        actor=actor,
        reaction="approve" if granted else "reject",
        graph=graph_name,
    )

    config = {"configurable": {"thread_id": task_id}}
    snapshot = await graph.aget_state(config)
    if not snapshot.tasks or not snapshot.tasks[0].interrupts:
        raise HTTPException(
            status_code=409,
            detail=f"task {task_id} is not paused at an approval interrupt",
        )

    approval_token = ""
    if granted:
        # The interrupt value is `ApprovalRequest.model_dump()` (or the
        # two-person confirm payload, a superset) — both carry
        # `action_class` + `target`. Mint the token those fields require.
        interrupt_value = snapshot.tasks[0].interrupts[0].value or {}
        action_class = interrupt_value.get("action_class")
        target = interrupt_value.get("target") or ""
        if not action_class or "." not in target:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"task {task_id} interrupt missing action_class/target "
                    "('server.method'); cannot mint approval token"
                ),
            )
        server, method = target.split(".", 1)
        signing_secret = get_settings().langgraph_approval_signing_key
        if not signing_secret:
            raise HTTPException(
                status_code=503,
                detail="langgraph_approval_signing_key not configured; cannot approve",
            )
        approval_token = _sign_approval_token(
            task_id, action_class, server, method, signing_secret=signing_secret
        )

    resume_value = {
        "granted": granted,
        "deferred": False,
        "approval_token": approval_token,
        "actor": actor,
    }
    final = await graph.ainvoke(Command(resume=resume_value), config=config)
    output = final.get("output")

    queue = getattr(request.app.state, "task_queue", None)
    if queue is not None and not task_id.startswith("smoke-"):
        post_resume = await graph.aget_state(config)
        still_paused = bool(post_resume.tasks and post_resume.tasks[0].interrupts)
        if still_paused:
            deadline = datetime.now(UTC) + timedelta(seconds=get_settings().approval_ttl_seconds)
            await queue.park_for_approval(task_id, deadline)
            slog.info("admin_verdict_reparked", granted=granted)
            return {"task_id": task_id, "status": "resumed", "output": output}
        await queue.resolve_approval(task_id, output)

    slog.info("admin_verdict_complete", granted=granted, has_output=output is not None)
    return {"task_id": task_id, "status": "complete", "output": output}


class VerdictBody(BaseModel):
    actor: str = "rob"


@router.post("/tasks/{task_id}/approve")
async def approve_task(
    task_id: str, request: Request, body: VerdictBody | None = None
) -> dict[str, Any]:
    """Approve a paused task — resume the interrupt with a granted verdict.

    Bearer-gated (`/admin/*`). Mints the HMAC approval token server-side
    so the caller (HA Companion automation) never holds the signing
    secret. A namespace-delete two-person task re-pauses after this first
    grant and reports `resumed`; a second approve executes it.
    """
    actor = body.actor if body else "rob"
    return await _resume_with_verdict(request, task_id, granted=True, actor=actor)


@router.post("/tasks/{task_id}/deny")
async def deny_task(
    task_id: str, request: Request, body: VerdictBody | None = None
) -> dict[str, Any]:
    """Deny a paused task — resume the interrupt with a rejected verdict.

    Bearer-gated (`/admin/*`). No signed token is needed: errand-runner
    refuses on `granted=False` before it verifies the token.
    """
    actor = body.actor if body else "rob"
    return await _resume_with_verdict(request, task_id, granted=False, actor=actor)


# ---- task usage stats ----


# Precedence for collapsing a task's distinct served groups to ONE
# representative group, so ``by_group`` counts each task once and its
# values still sum to ``total_tasks``. "Most escalated wins": a task that
# ran some nodes local and escalated others is attributed to ``claude``;
# among locals, the strongest model that served wins. Groups not listed
# sort last (least-escalated), so an unknown future group never masks a
# real escalation.
_GROUP_PRECEDENCE = ["claude", "local-spark-coder", "local-spark", "local-p40"]


def _representative_group(served: list[str]) -> str:
    """Pick the single representative group from a task's distinct served set.

    Highest-precedence (most escalated) group wins; unknown groups rank
    below all known ones. Caller guarantees ``served`` is non-empty.
    """

    def rank(g: str) -> int:
        return _GROUP_PRECEDENCE.index(g) if g in _GROUP_PRECEDENCE else len(_GROUP_PRECEDENCE)

    return min(served, key=rank)


def _group_breakdown(
    rows: list[tuple[str, list[str] | None, int]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Split completion counts into local-vs-escalated by model group.

    Each row is ``(agent, served_groups, count)``. ``served_groups`` is the
    real per-completion provenance the worker stamped on the row (the
    distinct ``effective_group`` set that actually served the task — so a
    runtime ``escalate=True`` or a Spark-down degrade IS reflected). When it
    is ``None`` (rows completed before the provenance migration, or where the
    process-local accumulator was lost to a mid-task restart) we fall back to
    the static ``AGENT_GROUP`` map for that agent — degrading to the old
    *configured-group* behavior rather than dropping the row.

    A task that touched several groups is counted once under its
    representative group (see ``_representative_group``) so ``by_group`` sums
    to ``total_tasks``. ``by_tier`` keys on ``local`` / ``escalated`` /
    ``(unknown)``; ``escalated`` means Claude actually served at least one of
    the task's calls.

    Returns ``(by_group, by_tier)``.
    """
    # AGENT_GROUP is keyed on the AgentId Literal; agent keys are plain strs
    # (they include the synthetic "(unknown)" bucket), so look up via a cast
    # and treat any miss as unknown.
    group_for: dict[str, str] = {str(a): g for a, g in AGENT_GROUP.items()}
    by_group: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for agent, served, count in rows:
        if served:
            group_key = _representative_group(served)
            tier = "escalated" if "claude" in served else "local"
        else:
            group = group_for.get(agent)
            if group is None:
                group_key = "(unknown)"
                tier = "(unknown)"
            else:
                group_key = group
                tier = "escalated" if group == "claude" else "local"
        by_group[group_key] = by_group.get(group_key, 0) + count
        by_tier[tier] = by_tier.get(tier, 0) + count
    return (
        dict(sorted(by_group.items(), key=lambda x: x[1], reverse=True)),
        dict(sorted(by_tier.items(), key=lambda x: x[1], reverse=True)),
    )


class UsageStats(BaseModel):
    """Task completion counts over the requested window.

    by_agent and by_source are derived from the task_queue table.
    target_agent is sourced from ``envelope->>'target_agent'`` — the
    triager writes it back to the envelope on claim, so done rows
    almost always carry it. The rare ``""`` / missing value is grouped
    under ``"(unknown)"``.

    by_group and by_tier come from the per-completion ``served_groups``
    provenance the worker stamps on each row (see ``_group_breakdown``) —
    so runtime escalations and Spark-down degrades ARE reflected. Rows
    without provenance (pre-migration, or accumulator lost to a mid-task
    restart) fall back to the static ``AGENT_GROUP`` map for that agent.
    """

    days: int
    total_tasks: int
    by_agent: dict[str, int]
    by_source: dict[str, int]
    by_group: dict[str, int]
    by_tier: dict[str, int]


@router.get("/cost", response_model=UsageStats)
async def get_usage_stats(request: Request, days: int = 7) -> UsageStats:
    """Task completion counts broken down by agent and source.

    Queries ``task_queue`` for rows with ``status='done'`` and
    ``created_at > NOW() - INTERVAL 'N days'``. ``target_agent`` is
    read from ``envelope->>'target_agent'`` (the triager stamps it on
    claim). ``source`` is from ``envelope->>'source'``.

    Phase 5 stub: Claude token spend is not yet tracked here — Langfuse
    integration lands in a later phase. For now this gives count-level
    visibility into task throughput by agent and ingress source.
    """
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    pool = request.app.state.queue_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="queue pool not initialized")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                COALESCE(NULLIF(envelope->>'target_agent', ''), '(unknown)') AS agent,
                COALESCE(NULLIF(envelope->>'source', ''), '(unknown)') AS source,
                served_groups,
                COUNT(*) AS cnt
            FROM task_queue
            WHERE status = 'done'
              AND created_at > NOW() - make_interval(days => %s)
            GROUP BY agent, source, served_groups
            """,
            (days,),
        )
        rows = await cur.fetchall()

    by_agent: dict[str, int] = {}
    by_source: dict[str, int] = {}
    # (agent, served_groups, count) rows for the local-vs-escalated breakdown.
    breakdown_rows: list[tuple[str, list[str] | None, int]] = []
    total = 0
    for agent, source, served_groups, cnt in rows:
        n = int(cnt)
        by_agent[agent] = by_agent.get(agent, 0) + n
        by_source[source] = by_source.get(source, 0) + n
        served = served_groups if isinstance(served_groups, list) else None
        breakdown_rows.append((agent, served, n))
        total += n

    sorted_by_agent = dict(sorted(by_agent.items(), key=lambda x: x[1], reverse=True))
    by_group, by_tier = _group_breakdown(breakdown_rows)

    return UsageStats(
        days=days,
        total_tasks=total,
        by_agent=sorted_by_agent,
        by_source=dict(sorted(by_source.items(), key=lambda x: x[1], reverse=True)),
        by_group=by_group,
        by_tier=by_tier,
    )


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
    JSONL log + the configured caps. The Windmill cost-cap-watcher reads this
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


# ---------------------------------------------------------------------------
# Smoke approval-flow endpoint
# ---------------------------------------------------------------------------
#
# `POST /admin/smoke/start-approval` starts a self-verifying smoke run that
# exercises the production approval flow (interrupt → ntfy → user tap →
# /approval resume → HMAC verify) without touching any real MCP server.
#
# Operator workflow:
#   1. POST /admin/smoke/start-approval  → returns {task_id}
#   2. (Windmill's langgraph-approval-post sweep sees the interrupt and
#      pushes the ntfy + magic link to the operator's phone)
#   3. Operator taps the magic link → /approval resumes the graph
#   4. errand-runner runs the smoke branch, writes JSON result envelope
#      into state.output
#   5. Driver polls GET /admin/tasks/<task_id> → reads `output` JSON
#
# The smoke graph (graphs/smoke.py) is single-node: START → errand-runner
# → END. It bypasses triager/supervisor so the only LLM cost is whatever
# errand-runner runs in its own node — currently zero (errand-runner is
# pure verification + execution).


class SmokeStartRequest(BaseModel):
    """Optional configuration for a smoke run.

    All fields default — POSTing `{}` is sufficient. The Windmill driver
    only needs the task_id back, which the endpoint generates.
    """

    label: str = Field(
        default="smoke",
        description=(
            "Operator-supplied label appended to the generated task_id. "
            "Useful for distinguishing multiple smoke runs in admin logs."
        ),
        max_length=32,
        pattern=r"^[a-zA-Z0-9_\-]+$",
    )


class SmokeStartResponse(BaseModel):
    task_id: str
    status: str = "interrupted"
    note: str = (
        "Graph paused at errand-runner. Approve via the ntfy magic link "
        "(Windmill langgraph-approval-post will push it). Poll "
        "/admin/tasks/<task_id> for the smoke result envelope."
    )


@router.post("/smoke/start-approval", response_model=SmokeStartResponse)
async def start_smoke_approval(req: SmokeStartRequest, request: Request) -> SmokeStartResponse:
    """Start a smoke-test approval flow run.

    Constructs a synthetic ApprovalRequest targeting the `smoke.test_write`
    pseudo-server, invokes the single-node smoke graph, and returns
    once the graph hits the interrupt at errand-runner. The rest of the
    flow (ntfy push, magic link, user tap, resume, HMAC verify, smoke
    write/readback/delete) happens through the same production code paths
    a real Class-C action takes.
    """
    smoke_graph = getattr(request.app.state, "smoke_graph", None)
    if smoke_graph is None:
        raise HTTPException(status_code=503, detail="smoke graph not initialized")

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    task_id = f"smoke-{req.label}-{ts}"

    approval_request = ApprovalRequest(
        action_class="C",
        target="smoke.test_write",
        payload_summary=(
            f"SMOKE TEST: write+readback+delete a marker file under "
            f"<vault_smoke_dir>/smoke-{task_id}.md. No external side effects."
        ),
        undo_path="errand-runner deletes the file in the same step",
        proposed_by="errand-runner",
        cost_estimate_usd=0.0,
    )

    initial_state = FleetState(
        task_id=task_id,
        source="test",
        content=(
            "Synthetic smoke test of the approval flow. The HMAC verify "
            "of the resume token is the load-bearing assertion."
        ),
        approval_request=approval_request,
        approval_granted=None,  # forces the interrupt path
    )

    config = {"configurable": {"thread_id": task_id}}

    # ainvoke returns when the graph hits the interrupt at errand-runner.
    # No exception is raised — the graph is paused and waiting for a
    # `Command(resume=...)` to come in via /approval.
    await smoke_graph.ainvoke(initial_state.model_dump(), config=config)

    # Fire the approval-post webhook so the operator gets the ntfy push
    # + Zulip card with the magic link to approve. The queue worker
    # does this automatically for queue-processed tasks; the smoke
    # endpoint bypasses the queue (invokes smoke_graph directly), so
    # without this explicit call the smoke would just sit in the
    # checkpointer until `langgraph-awaiting-user-sweep.ts` caught it
    # 30 minutes later. See PR #86 for the bug history.
    await post_approval_for_interrupts(
        smoke_graph,
        task_id,
        content=initial_state.content,
    )

    return SmokeStartResponse(task_id=task_id)
