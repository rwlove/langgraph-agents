"""CNPG-backed task queue primitives.

Per HOMELAB-SPEC Layer 5 task queue requirement + the substrate decision
in `docs/src/task_queue_substrate_design.md` (Option B — CNPG
LISTEN/NOTIFY) + the rollout plan's Phase 4.M1.

Lifecycle of one task:

    enqueue(envelope)            → status=pending
    dequeue(worker_id)           → status=claimed, visibility_timeout_at=now()+t
    ack(task_id)                 → status=done (or row deleted; see DELETE_ON_ACK)
    OR
    park_for_approval(id, exp)   → status=awaiting_approval (paused on an
                                   ApprovalRequest interrupt; Layer 4 Guardian)
        resolve_approval(id, out)→ status=done (verdict landed via /approval)
        OR expire_overdue_approvals() → row → task_dlq (guardian TTL lapsed)
    OR
    nack(task_id)                → status=pending, attempts++ (worker requeues)
    OR
    to_dlq(task_id, error)       → row moved to task_dlq

Worker crash: visibility_timeout_at passes → next dequeue() reclaims the
task and bumps attempts. Honors envelope.retry_policy.max_attempts (or
the chart-wide default) before routing to DLQ.

NOTIFY: enqueue() sends `NOTIFY task_queue_new` so workers waiting on
LISTEN wake up immediately. The worker loop (Phase 4.M2) combines LISTEN
with periodic polling for visibility-timeout reclaim.

This module is async (psycopg-pool). The connection pool is the same
one the LangGraph checkpointer uses, passed in by `agents.main` at
startup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ulid import ULID

from agents.observability import get_logger

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

NOTIFY_CHANNEL = "task_queue_new"
_DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 300

logger = logging.getLogger("agents.queue")
slog = get_logger("queue")


@dataclass(frozen=True)
class TaskClaim:
    """A claimed task ready for processing.

    Returned by `TaskQueue.dequeue`. The caller is responsible for
    calling either `ack(task_id)` on success or `to_dlq(task_id, error)`
    / `nack(task_id)` on failure. If the worker crashes before either
    call, the row's `visibility_timeout_at` ensures another worker
    reclaims it.
    """

    task_id: str
    envelope: dict[str, Any]
    attempts: int


class TaskQueue:
    """Thin async wrapper over the `task_queue` + `task_dlq` tables.

    Construct with a pool, share across requests. All methods are
    coroutines.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        default_visibility_timeout_seconds: int = _DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        default_max_attempts: int = 3,
    ) -> None:
        self._pool = pool
        self._default_visibility_timeout = default_visibility_timeout_seconds
        self._default_max_attempts = default_max_attempts

    async def enqueue(self, envelope: dict[str, Any]) -> str:
        """Insert a new task, return the generated ULID.

        Raises `psycopg.errors.UniqueViolation` if `envelope['idempotency_key']`
        collides with an existing pending/claimed/done row — the caller
        handles dedup at a higher layer (Dragonfly fast-path in
        `agents.idempotency`; Postgres is defense-in-depth).
        """
        task_id = str(ULID())
        ttl_seconds = envelope.get("ttl_seconds")
        ttl_expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(ttl_seconds)) if ttl_seconds else None
        )

        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO task_queue (id, envelope, ttl_expires_at)
                VALUES (%s, %s::jsonb, %s)
                """,
                (task_id, json.dumps(envelope), ttl_expires_at),
            )
            # `NOTIFY` is a SQL command, not a function, and Postgres's
            # parser rejects bind parameters in its payload slot
            # (`syntax error at or near "$1"`). The `pg_notify(text, text)`
            # function accepts parameters because it's an ordinary
            # function call. Same wakeup semantics on the LISTEN side.
            await conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, task_id))

        slog.info(
            "task_enqueued",
            task_id=task_id,
            ttl_seconds=ttl_seconds,
            idempotency_key=envelope.get("idempotency_key"),
        )
        return task_id

    async def dequeue(
        self,
        worker_id: str,
        *,
        visibility_timeout_seconds: int | None = None,
    ) -> TaskClaim | None:
        """Atomically claim the oldest pending task, OR reclaim a task
        whose visibility timeout has passed.

        Uses `SELECT ... FOR UPDATE SKIP LOCKED` for safe concurrent
        workers — each worker gets a distinct row without contention.

        Returns `None` when the queue is empty.
        """
        timeout = visibility_timeout_seconds or self._default_visibility_timeout
        visibility_at = datetime.now(UTC) + timedelta(seconds=timeout)

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE task_queue
                SET status = 'claimed',
                    claimed_at = NOW(),
                    claimed_by = %s,
                    visibility_timeout_at = %s,
                    attempts = attempts + 1,
                    updated_at = NOW()
                WHERE id = (
                    SELECT id FROM task_queue
                    WHERE (
                        status = 'pending'
                        OR (status = 'claimed' AND visibility_timeout_at < NOW())
                    )
                    -- TTL guard: never claim a task past its deadline. Per
                    -- HOMELAB-SPEC Layer 5 an expired task must NOT auto-
                    -- execute; the sweeper (`expire_overdue`) terminates it.
                    AND (ttl_expires_at IS NULL OR ttl_expires_at > NOW())
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, envelope, attempts
                """,
                (worker_id, visibility_at),
            )
            row = await cur.fetchone()

        if row is None:
            return None

        task_id, envelope, attempts = row
        slog.info(
            "task_dequeued",
            task_id=task_id,
            worker_id=worker_id,
            attempts=attempts,
        )
        return TaskClaim(task_id=task_id, envelope=envelope, attempts=attempts)

    async def ack(self, task_id: str) -> None:
        """Mark task done."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE task_queue SET status = 'done', updated_at = NOW() WHERE id = %s",
                (task_id,),
            )
        slog.info("task_acked", task_id=task_id)

    async def park_for_approval(
        self,
        task_id: str,
        approval_expires_at: datetime,
        approval_request: dict[str, Any] | None = None,
    ) -> None:
        """Park a claimed task at `awaiting_approval` instead of acking done.

        Called by the worker when the graph paused on an ApprovalRequest
        interrupt (HOMELAB-SPEC Layer 4 Guardian). Before this, such a
        task was acked `done` — the durable row lied about a task that
        was actually waiting on Rob in the checkpointer. `approval_expires_at`
        carries the Layer 5 guardian TTL; `expire_overdue_approvals`
        enforces it.

        `approval_request` is the curated decision subset (see
        `approval_post.curate_approval_request`) persisted on the row so
        the guardian listing can render what Rob is approving without a
        checkpointer scan. NULL-safe: a None leaves the column unchanged
        so the defer-path re-park doesn't clobber an already-stored
        request.

        Idempotent under at-least-once resume: re-parking an already-parked
        row just refreshes `approval_expires_at` (used by the defer path).
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'awaiting_approval',
                    approval_expires_at = %s,
                    approval_request = COALESCE(%s::jsonb, approval_request),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    approval_expires_at,
                    json.dumps(approval_request) if approval_request is not None else None,
                    task_id,
                ),
            )
        slog.info(
            "task_awaiting_approval",
            task_id=task_id,
            approval_expires_at=approval_expires_at.isoformat(),
        )

    async def resolve_approval(self, task_id: str, output: str | None) -> None:
        """Ack an awaiting-approval task `done` once the verdict resolves.

        Called by the `/approval` resume path (api/approval.py) after the
        graph runs to completion on approve/reject. Clears
        `approval_expires_at` so a resolved row is never swept.

        No-op-safe: if the row already left `awaiting_approval` (e.g. the
        guardian sweep moved it to the DLQ on TTL expiry before a late
        resume landed), the `WHERE` clause matches nothing and the late
        resume is harmless.
        """
        result_payload = json.dumps({"output": output}) if output is not None else None
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'done',
                    result = %s::jsonb,
                    approval_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND status = 'awaiting_approval'
                """,
                (result_payload, task_id),
            )
        slog.info("task_approval_resolved", task_id=task_id, has_output=output is not None)

    async def nack(self, task_id: str) -> None:
        """Return a claim to pending. Worker requeues without DLQ.

        Use when the failure is transient (network blip, downstream
        503, etc.). For terminal failures, call `to_dlq` instead.
        """
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'pending',
                    claimed_at = NULL,
                    claimed_by = NULL,
                    visibility_timeout_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (task_id,),
            )
        slog.info("task_nacked", task_id=task_id)

    async def to_dlq(self, task_id: str, error: str) -> None:
        """Move task to DLQ. Terminal — no further automatic retry.

        4.M3's DLQ surface (Windmill workflow + Zulip #dlq) reads from
        the `task_dlq` table and surfaces these for operator review.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                WITH source AS (
                    DELETE FROM task_queue WHERE id = %s
                    RETURNING id, envelope, attempts
                )
                INSERT INTO task_dlq (id, envelope, attempts, last_error)
                SELECT id, envelope, attempts, %s FROM source
                """,
                (task_id, error),
            )
        slog.warning("task_to_dlq", task_id=task_id, error=error[:200])

    async def expire_overdue(self) -> list[TaskClaim]:
        """Terminate tasks past their TTL — they do NOT auto-execute.

        Per HOMELAB-SPEC Layer 5: "On expiry: the task does not auto-
        execute; it expires and notifies Rob with a summary." This is
        the enforcement half — `dequeue`'s TTL guard ensures an expired
        task is never claimed, and this sweep moves the dead rows to the
        DLQ (the 4.M3 operator-review surface) tagged `ttl_expired`, so
        Rob sees *why* a task never ran rather than it silently
        vanishing.

        Catches both never-claimed (`pending`) and stuck-claim
        (`claimed`, worker vanished mid-flight and the visibility timeout
        outlived the TTL) rows. Atomic CTE — the DELETE + DLQ INSERT
        commit together, so a task is never lost between tables.

        Returns the expired claims so the caller can build the operator
        summary. Empty list when nothing was overdue.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                WITH overdue AS (
                    DELETE FROM task_queue
                    WHERE status IN ('pending', 'claimed')
                      AND ttl_expires_at IS NOT NULL
                      AND ttl_expires_at < NOW()
                    RETURNING id, envelope, attempts
                )
                INSERT INTO task_dlq (id, envelope, attempts, last_error)
                SELECT id, envelope, attempts, 'ttl_expired' FROM overdue
                RETURNING id, envelope, attempts
                """
            )
            rows = await cur.fetchall()

        expired = [TaskClaim(task_id=r[0], envelope=r[1], attempts=r[2]) for r in rows]
        for claim in expired:
            slog.warning("task_ttl_expired", task_id=claim.task_id)
        return expired

    async def expire_overdue_approvals(self) -> list[TaskClaim]:
        """Expire awaiting-approval tasks past their guardian TTL.

        Per HOMELAB-SPEC Layer 5 Guardian: "On expiry: the task does not
        auto-execute; it expires and notifies Rob with a summary." This is
        the approval-queue analogue of `expire_overdue` — it sweeps rows
        parked at `awaiting_approval` whose `approval_expires_at` passed,
        moving them to the DLQ tagged `approval_ttl_expired` so Rob sees a
        lapsed approval rather than it silently vanishing.

        The checkpointer thread is deliberately left paused; the
        durable-queue row is the source of truth, and `resolve_approval`
        is a no-op on the now-absent row, so a late `/approval` resume
        after expiry can't execute the action. Atomic CTE — DELETE + DLQ
        INSERT commit together.

        Returns the expired claims for the operator summary. Empty list
        when nothing was overdue.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                WITH overdue AS (
                    DELETE FROM task_queue
                    WHERE status = 'awaiting_approval'
                      AND approval_expires_at IS NOT NULL
                      AND approval_expires_at < NOW()
                    RETURNING id, envelope, attempts
                )
                INSERT INTO task_dlq (id, envelope, attempts, last_error)
                SELECT id, envelope, attempts, 'approval_ttl_expired' FROM overdue
                RETURNING id, envelope, attempts
                """
            )
            rows = await cur.fetchall()

        expired = [TaskClaim(task_id=r[0], envelope=r[1], attempts=r[2]) for r in rows]
        for claim in expired:
            slog.warning("task_approval_ttl_expired", task_id=claim.task_id)
        return expired

    async def awaiting_approval_stats(self) -> tuple[int, float]:
        """Return (depth, oldest_age_seconds) for the awaiting-approval queue.

        `depth` is the count of rows parked at `awaiting_approval`.
        `oldest_age_seconds` is the wall-clock age of the oldest such row
        (by `updated_at`, the moment it was parked), 0.0 when none are
        parked. Backed by the `idx_task_queue_awaiting` partial index, so
        this is a cheap aggregate the worker can run every approval sweep
        to feed the queue-depth/age gauges.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(updated_at))), 0)
                FROM task_queue
                WHERE status = 'awaiting_approval'
                """
            )
            row = await cur.fetchone()

        if row is None:
            return (0, 0.0)
        return (int(row[0]), float(row[1]))

    async def attempts_remaining(self, claim: TaskClaim) -> int:
        """Return retries remaining per `envelope.retry_policy.max_attempts`,
        or the default if the envelope doesn't specify one.
        """
        max_attempts = self._default_max_attempts
        policy = claim.envelope.get("retry_policy")
        if isinstance(policy, dict):
            max_attempts = int(policy.get("max_attempts", max_attempts))
        return max(0, max_attempts - claim.attempts)
