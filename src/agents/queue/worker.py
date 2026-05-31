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
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract

from agents.observability import (
    clear_task_provenance,
    get_logger,
    langgraph_awaiting_approval_oldest_age_seconds,
    langgraph_awaiting_approval_tasks,
    served_groups_for,
)
from agents.queue.approval_post import has_pending_approval, post_approval_for_interrupts
from agents.router import estimate_input_tokens
from agents.settings import get_settings
from agents.state import FleetState
from agents.tools import pushover
from agents.tools.zulip import ZulipNotConfiguredError, send_dm

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from agents.queue.store import TaskClaim, TaskQueue

logger = logging.getLogger("agents.queue.worker")
slog = get_logger("queue.worker")


def _extract_trace_context(envelope: dict[str, Any]) -> Context | None:
    """Rebuild the ingress OTel context the inbox injected into the envelope.

    Lets this worker's `queue.process` span continue the trace started at
    /inbox instead of opening an orphan root (the gap recorded in home-ops
    DoD 2026-05-31). Returns None when the envelope carries no carrier —
    directly-enqueued tasks (tests, legacy callers) and any backlog
    enqueued before this fix — so the span cleanly falls back to a root.
    Guarded so a malformed carrier can never fail task processing.
    """
    carrier = envelope.get("otel_carrier")
    if not isinstance(carrier, dict):
        return None
    try:
        return extract(carrier)
    except Exception:
        logger.exception("OTel trace-context extraction failed")
        return None


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
        sweep_interval_seconds: float = 60.0,
    ) -> None:
        self._queue = queue
        self._pool = pool
        self._graph = graph
        self._idle_poll = idle_poll_seconds
        self._sweep_interval = sweep_interval_seconds
        # monotonic timestamp of the last TTL sweep; 0.0 forces a sweep on
        # the first loop iteration.
        self._last_sweep = 0.0
        # Independent throttle for the guardian approval-TTL sweep, so the
        # two sweeps don't share a clock and starve one another.
        self._last_approval_sweep = 0.0
        self._worker_id = _worker_id()
        self._stopping = False

    async def run(self) -> None:
        """Main loop. Returns when stop() is called or asyncio cancels."""
        slog.info("queue_worker_started", worker_id=self._worker_id)
        try:
            while not self._stopping:
                await self._maybe_sweep_expired()
                await self._maybe_sweep_expired_approvals()
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

    async def _maybe_sweep_expired(self) -> None:
        """Run the TTL-expiry sweep at most once per `sweep_interval`.

        Throttled off the dequeue cadence so a busy queue (which loops
        with no idle sleep) doesn't hammer the sweep query every
        iteration. A DB hiccup in the sweep is logged and swallowed —
        the worker must keep draining real work regardless.
        """
        now = time.monotonic()
        if now - self._last_sweep < self._sweep_interval:
            return
        self._last_sweep = now
        try:
            expired = await self._queue.expire_overdue()
        except Exception as exc:  # pool / DB hiccup
            slog.warning("queue_worker_sweep_error", error=str(exc))
            return
        if expired:
            await self._notify_expired(expired)

    async def _notify_expired(self, expired: list[TaskClaim]) -> None:
        """Notify Rob that TTL-expired tasks were moved to the DLQ.

        HOMELAB-SPEC Layer 5 requires expiry to surface a summary to
        Rob, not silently drop. Pushover is the established Tier-1
        escalation channel (shared with Alertmanager). Best-effort: a
        Pushover failure must not stall the loop — the rows are already
        durably in the DLQ for the 4.M3 review surface either way.
        """
        count = len(expired)
        lines: list[str] = []
        for claim in expired[:10]:
            intent = str(claim.envelope.get("intent") or claim.envelope.get("content") or "")
            lines.append(f"• {claim.task_id}: {intent[:80]}")
        if count > 10:
            lines.append(f"… and {count - 10} more")
        message = (
            f"{count} task(s) hit their TTL before running and were moved to "
            f"the DLQ for review (they did NOT execute):\n" + "\n".join(lines)
        )
        slog.warning("task_ttl_expired_batch", count=count)
        try:
            await asyncio.to_thread(
                pushover.send,
                "Tasks expired (TTL)",
                message,
                priority=1,
            )
        except Exception as exc:  # pushover unconfigured / network
            slog.warning("queue_worker_expire_notify_failed", error=str(exc))

    async def _maybe_sweep_expired_approvals(self) -> None:
        """Run the guardian approval-TTL sweep at most once per interval.

        HOMELAB-SPEC Layer 4 Guardian + Layer 5: a task parked at
        `awaiting_approval` that outlives its `approval_expires_at`
        deadline is moved to the DLQ tagged `approval_ttl_expired`. The
        task does NOT auto-execute — the LangGraph thread is left paused
        in the checkpointer; only the durable queue row moves. Throttled
        and error-swallowing for the same reasons as the TTL sweep: the
        worker must keep draining real work regardless.
        """
        now = time.monotonic()
        if now - self._last_approval_sweep < self._sweep_interval:
            return
        self._last_approval_sweep = now
        try:
            expired = await self._queue.expire_overdue_approvals()
        except Exception as exc:  # pool / DB hiccup
            slog.warning("queue_worker_approval_sweep_error", error=str(exc))
            return
        if expired:
            await self._notify_expired_approvals(expired)
        await self._refresh_approval_gauges()

    async def _refresh_approval_gauges(self) -> None:
        """Publish guardian-queue depth + oldest-age to Prometheus.

        Runs after the expiry sweep so the gauges reflect the queue with
        lapsed rows already reaped. Best-effort: a stats-query failure must
        not stall the loop, so the gauges simply hold their last value.
        """
        try:
            depth, oldest_age = await self._queue.awaiting_approval_stats()
        except Exception as exc:  # pool / DB hiccup
            slog.warning("queue_worker_approval_stats_error", error=str(exc))
            return
        langgraph_awaiting_approval_tasks.set(depth)
        langgraph_awaiting_approval_oldest_age_seconds.set(oldest_age)

    async def _notify_expired_approvals(self, expired: list[TaskClaim]) -> None:
        """Notify Rob that awaiting-approval tasks lapsed past their TTL.

        Distinct from `_notify_expired`: these tasks DID reach Rob's
        guardian queue and asked for a verdict — they expired because the
        verdict never came, not because they timed out before running.
        Per HOMELAB-SPEC Layer 5 the expiry must surface a summary, never
        silently drop. Best-effort Pushover (Tier-1 channel); a send
        failure must not stall the loop — the rows are durably in the DLQ.
        """
        count = len(expired)
        lines: list[str] = []
        for claim in expired[:10]:
            intent = str(claim.envelope.get("intent") or claim.envelope.get("content") or "")
            lines.append(f"• {claim.task_id}: {intent[:80]}")
        if count > 10:
            lines.append(f"… and {count - 10} more")
        message = (
            f"{count} task(s) awaiting your approval lapsed past their TTL and "
            f"were moved to the DLQ (they did NOT execute):\n" + "\n".join(lines)
        )
        slog.warning("approval_ttl_expired_batch", count=count)
        try:
            await asyncio.to_thread(
                pushover.send,
                "Approvals expired (TTL)",
                message,
                priority=1,
            )
        except Exception as exc:  # pushover unconfigured / network
            slog.warning("queue_worker_approval_expire_notify_failed", error=str(exc))

    async def _handle_one(self, claim: TaskClaim) -> None:
        """Process one claim: invoke the graph, ack / nack / dlq."""
        envelope = claim.envelope
        task_id = claim.task_id
        # Wall-clock duration for completion-post notifications.
        started_monotonic = time.monotonic()

        # Per-task contextvars — same shape as /inbox bound when it was
        # synchronous. Per-node binding (agent, ...) happens inside the
        # graph wrapper.
        ctx: dict[str, Any] = {
            "task_id": task_id,
            "source": envelope.get("source", "test"),
            "user": envelope.get("user", "rob"),
            "data_tier": envelope.get("data_tier", "internal"),
            "est_input_tokens": estimate_input_tokens(envelope.get("content", "")),
            "destructive": bool(envelope.get("destructive")),
        }
        # trace_id is minted at ingress (api/inbox._ensure_trace_id); bind
        # it so every node + worker log line for this task — and the
        # `app.trace_id` span attribute set in the envelope loop below —
        # share the ingress id. Omitted for directly-enqueued tasks
        # (tests, legacy callers) that never went through /inbox.
        trace_id = envelope.get("trace_id")
        if trace_id:
            ctx["trace_id"] = trace_id
        structlog.contextvars.bind_contextvars(**ctx)

        # OTel: the FastAPI auto-span is gone (we're not in /inbox
        # anymore). Continue the ingress trace when the envelope carries a
        # propagated carrier; otherwise start a clean worker-side root span.
        tracer = trace.get_tracer("agents.queue.worker")
        parent_ctx = _extract_trace_context(envelope)
        with tracer.start_as_current_span("queue.process", context=parent_ctx) as span:
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

        # Detect approval-interrupt before acking. When a specialist
        # called interrupt() with an ApprovalRequest payload, the graph
        # returns with no `output`; the request is exposed via the
        # checkpointer's interrupts list. Such a task is NOT done — it is
        # parked waiting on Rob's verdict (HOMELAB-SPEC Layer 4 Guardian).
        # Acking it `done` here (the historical bug) made the durable
        # queue lie about a task still alive in the checkpointer.
        #
        # `has_pending_approval` is webhook-independent so a deployment
        # with no approval webhook still parks correctly. It swallows
        # checkpointer errors (returns False) so the dispatch loop never
        # crashes on a detection read — a missed park is caught by
        # langgraph-awaiting-user-sweep.ts at the 30-min escalation tier.
        if await has_pending_approval(self._graph, task_id):
            # Fire the Windmill approval webhook so ntfy + Zulip cards
            # surface immediately (best-effort, webhook-gated internally).
            await post_approval_for_interrupts(
                self._graph,
                task_id,
                content=envelope.get("content") or "",
                trace_id=envelope.get("trace_id"),
            )
            # Park the durable row at `awaiting_approval` with the Layer 5
            # guardian TTL. The /approval resume path acks it `done`; the
            # guardian sweep expires it to the DLQ if Rob never answers.
            await self._queue.park_for_approval(task_id, self._approval_deadline(envelope))
            return

        await self._ack_with_result(task_id, output)

        # Completion DM. Skipped implicitly when the graph paused for
        # approval — pausing produces no `output`, so the `if output:`
        # check is enough; the approval-post webhook already fired its
        # own notification. Fire when the task is genuinely finished
        # (output produced).
        if output:
            duration_s = time.monotonic() - started_monotonic
            await self._post_completion(task_id, envelope, output, duration_s)

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
            # Caller-pinned target agent (Windmill workflows that know
            # the right specialist). When set, triager_node respects it
            # and skips its own routing — see agents.nodes.triager.
            target_agent=envelope.get("target_agent"),
        )
        thread_id = envelope.get("conversation_id") or task_id
        config: Any = {"configurable": {"thread_id": thread_id}}
        final = await self._graph.ainvoke(initial_state, config=config)
        output = final.get("output") if isinstance(final, dict) else None
        return output if isinstance(output, str) else None

    async def _post_completion(
        self,
        task_id: str,
        envelope: dict[str, Any],
        output: str,
        duration_s: float,
    ) -> None:
        """POST a completion card to the Windmill completion-post webhook.

        Stage 2 of HomeAIOps stabilization: fire-and-forget DM to Rob
        when a task finishes successfully, so he doesn't have to poll
        `hai task ls`. Mirrors `_post_approval_for_interrupts` —
        best-effort, no exceptions propagate. Skipped when the
        webhook URL is unset (backward-compat path; pre-Stage 2 home-
        ops deployments).
        """
        settings = get_settings()
        webhook_url = settings.completion_post_webhook_url
        if not webhook_url:
            return

        # Windmill function signature:
        #   main(task_id, target_agent?, content?, output?, duration_s?)
        # — top-level body keys map to positional args by name.
        # target_agent is read from the checkpointer snapshot so a
        # truthful "which agent finished" value is always present, not
        # whatever was in the envelope hint (the supervisor may have
        # routed differently than the requester guessed).
        target_agent: str | None = None
        try:
            snapshot = await self._graph.aget_state({"configurable": {"thread_id": task_id}})
            if snapshot and snapshot.values:
                target_agent = snapshot.values.get("target_agent")
        except Exception:
            logger.exception("completion-post: aget_state failed task=%s", task_id)

        payload = {
            "task_id": task_id,
            "trace_id": envelope.get("trace_id") or "",
            "target_agent": target_agent or "",
            "content": envelope.get("content") or "",
            "output": output,
            "duration_s": round(duration_s, 1),
        }
        headers: dict[str, str] = {}
        if settings.completion_post_webhook_token:
            headers["Authorization"] = f"Bearer {settings.completion_post_webhook_token}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload, headers=headers)
        except Exception:
            logger.exception("completion-post: webhook call failed task=%s", task_id)
            return

        if resp.status_code >= 400:
            logger.warning(
                "completion-post: webhook %d task=%s body=%.200s",
                resp.status_code,
                task_id,
                resp.text,
            )
        else:
            slog.info(
                "completion_post",
                task_id=task_id,
                status=resp.status_code,
                duration_s=duration_s,
                target_agent=target_agent,
            )

    @staticmethod
    def _approval_deadline(envelope: dict[str, Any]) -> datetime:
        """Compute the guardian-TTL deadline for an awaiting-approval task.

        Honors an explicit `envelope['ttl_seconds']` when present (the
        task carried its own deadline through the contract); otherwise
        falls back to the cluster-wide `approval_ttl_seconds` default
        (24h). Per HOMELAB-SPEC Layer 5 the deadline drives expiry +
        notify, never auto-execution.
        """
        ttl_seconds = envelope.get("ttl_seconds")
        if not ttl_seconds:
            ttl_seconds = get_settings().approval_ttl_seconds
        return datetime.now(UTC) + timedelta(seconds=int(ttl_seconds))

    async def _ack_with_result(self, task_id: str, output: str | None) -> None:
        """Mark task done and store the output for `/admin/tasks/<id>` retrieval.

        Also drains the process-local model-group provenance accumulated for
        this task (recorded in ``agents.llm`` at each model-build site) onto
        the row, so the cost breakdown reports the group(s) that ACTUALLY
        served — including runtime escalation / Spark-down degrade — rather
        than deriving from the static AGENT_GROUP map. Empty (e.g. accumulator
        lost to a pod restart mid-task) writes NULL, and the read path falls
        back to AGENT_GROUP for NULL rows.
        """
        result_payload = json.dumps({"output": output}) if output is not None else None
        served = served_groups_for(task_id)
        served_payload = json.dumps(served) if served else None
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE task_queue
                SET status = 'done',
                    result = %s::jsonb,
                    served_groups = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (result_payload, served_payload, task_id),
            )
        clear_task_provenance(task_id)
        slog.info(
            "task_completed",
            task_id=task_id,
            has_output=output is not None,
            served_groups=served,
        )
