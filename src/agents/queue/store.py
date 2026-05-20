"""CNPG-backed task queue primitives.

Per HOMELAB-SPEC Layer 5 task queue requirement + the substrate decision
in `docs/src/task_queue_substrate_design.md` (Option B — CNPG
LISTEN/NOTIFY) + the rollout plan's Phase 4.M1.

Lifecycle of one task:

    enqueue(envelope)            → status=pending
    dequeue(worker_id)           → status=claimed, visibility_timeout_at=now()+t
    ack(task_id)                 → status=done (or row deleted; see DELETE_ON_ACK)
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
            await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}, %s", (task_id,))

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
                    WHERE status = 'pending'
                       OR (status = 'claimed' AND visibility_timeout_at < NOW())
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

    async def attempts_remaining(self, claim: TaskClaim) -> int:
        """Return retries remaining per `envelope.retry_policy.max_attempts`,
        or the default if the envelope doesn't specify one.
        """
        max_attempts = self._default_max_attempts
        policy = claim.envelope.get("retry_policy")
        if isinstance(policy, dict):
            max_attempts = int(policy.get("max_attempts", max_attempts))
        return max(0, max_attempts - claim.attempts)
