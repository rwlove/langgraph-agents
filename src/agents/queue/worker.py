"""Async queue worker (Phase 4.M2).

Runs as a background asyncio task in the same process as the FastAPI
app. Drains `task_queue` by calling the LangGraph fleet graph and
preserves the Zulip-DM-back behavior the synchronous `/inbox` had.

Lifecycle:
- Started in `agents.main.lifespan` after the graph is compiled.
- Stopped on shutdown via `asyncio.Task.cancel()`.

Loop body:
1. Try `queue.dequeue(worker_id)`. If None, sleep `idle_poll_seconds`
   and retry.
2. Bind structlog contextvars for the task (task_id, source, user,
   data_tier) so logs + spans inherit them.
3. Invoke the fleet graph against `FleetState.from_envelope(...)`.
4. On success:
   - `queue.ack(task_id)` and write `result` for poll retrieval.
   - Zulip DM-back if envelope.source=="zulip" + zulip_user_id set.
5. On exception:
   - If retries remain per envelope.retry_policy: `queue.nack(task_id)`.
   - Else: `queue.to_dlq(task_id, error)`.

Crash recovery is handled by the visibility timeout — a worker that
disappears leaves its claim to expire, then the next dequeue reclaims.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace

from agents.observability import get_logger
from agents.state import FleetState
from agents.tools.zulip import ZulipNotConfiguredError, send_dm

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from agents.queue.store import TaskClaim, TaskQueue

logger = logging.getLogger("agents.queue.worker")
slog = get_logger("queue.worker")


def _worker_id() -> str:
    """Per-process identifier — pod name + pid for uniqueness across
    rolling deployments + restart attempts within a pod.
    """
    pod = os.getenv("HOSTNAME", socket.gethostname())
    return f"{pod}/{os.getpid()}"


class QueueWorker:
    """Single-loop async worker that drains the task queue.

    Multiple instances (e.g., increased replicas) coexist safely —
    `dequeue` uses `FOR UPDATE SKIP LOCKED` so each gets distinct rows.
    """

    def __init__(
        self,
        queue: TaskQueue,
        pool: AsyncConnectionPool,
        graph: Any,  # CompiledStateGraph; langgraph generic typing is fluid
        *,
        idle_poll_seconds: float = 2.0,
    ) -> None:
        self._queue = queue
        self._pool = pool
        self._graph = graph
        self._idle_poll = idle_poll_seconds
        self._worker_id = _worker_id()
        self._stopping = False

    async def run(self) -> None:
        """Main loop. Returns when stop() is called or asyncio cancels."""
        slog.info("queue_worker_started", worker_id=self._worker_id)
        try:
            while not self._stopping:
                try:
                    claim = await self._queue.dequeue(self._worker_id)
                except Exception as exc:  # pool / DB hiccup
                    slog.warning("queue_worker_dequeue_error", error=str(exc))
                    await asyncio.sleep(self._idle_poll)
                    continue

                if claim is None:
                    await asyncio.sleep(self._idle_poll)
                    continue

                await self._handle_one(claim)
        finally:
            slog.info("queue_worker_stopped", worker_id=self._worker_id)

    async def stop(self) -> None:
        """Signal the loop to exit at the next iteration."""
        self._stopping = True

    async def _handle_one(self, claim: TaskClaim) -> None:
        """Process one claim: invoke the graph, ack / nack / dlq."""
        envelope = claim.envelope
        task_id = claim.task_id

        # Per-task contextvars — same shape as /inbox bound when it was
        # synchronous. Per-node binding (agent, ...) happens inside the
        # graph wrapper.
        structlog.contextvars.bind_contextvars(
            task_id=task_id,
            source=envelope.get("source", "test"),
            user=envelope.get("user", "rob"),
            data_tier=envelope.get("data_tier", "internal"),
        )

        # OTel: the FastAPI auto-span is gone (we're not in /inbox
        # anymore); start a worker-side root span.
        tracer = trace.get_tracer("agents.queue.worker")
        with tracer.start_as_current_span("queue.process") as span:
            for k, v in envelope.items():
                if v is None or isinstance(v, dict | list):
                    continue
                span.set_attribute(f"app.{k}", v)
            span.set_attribute("app.task_id", task_id)
            span.set_attribute("app.attempts", claim.attempts)

            try:
                output = await self._invoke_graph(task_id, envelope)
            except Exception as exc:
                logger.exception("queue worker graph invoke failed task=%s", task_id)
                remaining = await self._queue.attempts_remaining(claim)
                if remaining > 0:
                    await self._queue.nack(task_id)
                    slog.info(
                        "task_nack_retry",
                        task_id=task_id,
                        attempts=claim.attempts,
                        remaining=remaining,
                    )
                else:
                    await self._queue.to_dlq(task_id, repr(exc))
                return

        await self._ack_with_result(task_id, output)

        # Zulip DM-back if applicable — preserves the synchronous
        # /inbox's behavior so the triager bot keeps replying in DMs.
        if envelope.get("source") == "zulip" and envelope.get("zulip_user_id") and output:
            try:
                result = send_dm(int(envelope["zulip_user_id"]), output)
                logger.info(
                    "zulip-reply task=%s user_id=%s status=%s msg_id=%s",
                    task_id,
                    envelope["zulip_user_id"],
                    result.status_code,
                    result.msg_id,
                )
            except ZulipNotConfiguredError as exc:
                logger.warning("zulip-reply skipped (config missing) task=%s: %s", task_id, exc)
            except Exception:
                logger.exception("zulip-reply unexpected failure task=%s", task_id)

    async def _invoke_graph(
        self,
        task_id: str,
        envelope: dict[str, Any],
    ) -> str | None:
        """Build the initial FleetState from the envelope and run the graph."""
        initial_state = FleetState(
            task_id=task_id,
            source=envelope.get("source", "test"),
            content=envelope["content"],
            user=envelope.get("user", "rob"),
            trace_id=envelope.get("trace_id"),
            origin=envelope.get("origin"),
            requester=envelope.get("requester"),
            intent_envelope=envelope.get("intent"),
            priority=envelope.get("priority", "normal"),
            destructive=envelope.get("destructive"),
            idempotency_key=envelope.get("idempotency_key"),
            ttl_seconds=envelope.get("ttl_seconds"),
            data_tier=envelope.get("data_tier", "internal"),
        )
        config: Any = {"configurable": {"thread_id": task_id}}
        final = await self._graph.ainvoke(initial_state, config=config)
        output = final.get("output") if isinstance(final, dict) else None
        return output if isinstance(output, str) else None

    async def _ack_with_result(self, task_id: str, output: str | None) -> None:
        """Mark task done and store the output for `/admin/tasks/<id>` retrieval."""
        result_payload = json.dumps({"output": output}) if output is not None else None
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'done',
                    result = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (result_payload, task_id),
            )
        slog.info(
            "task_completed",
            task_id=task_id,
            has_output=output is not None,
        )
