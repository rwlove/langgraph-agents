"""Sweep the checkpointer for threads paused at `interrupt()`.

P3.7 — diagnostic-only sweep. Today no node calls `interrupt()` (the
wiring lands in P1.2), so this sweep will routinely find zero threads.
The value is twofold:

1. **Startup visibility.** On pod startup we run the sweep and log a
   warning per stale paused thread (older than
   ``stale_after_seconds``). If the pod restarted while threads were
   parked at an approval, the operator sees the count + IDs in the
   `agents.paused_threads` Loki stream.
2. **Wiring for resume-on-startup.** Once P1.2 lands and `interrupt()`
   is actually fired by class-C/D nodes, this same sweep grows an
   auto-resume code path (gated on a setting). Build the read side
   first so we can develop the resume side against a known-good
   inventory.

The sweep is **read-only**: it never mutates state, never resumes a
thread, never deletes a checkpoint. Resume logic is a deliberate
next-phase add — see ``# TODO(P1.2 follow-up)`` markers below for
where it slots in.

## Implementation

LangGraph's checkpointer exposes ``alist({})`` which yields a
``CheckpointTuple`` per checkpoint across all threads. We dedupe to the
**latest** checkpoint per ``thread_id`` (the first hit wins because
alist orders newest-first) and then call ``graph.aget_state`` to get
the resolved snapshot — same pattern as ``/admin/tasks``.

A thread is "paused" if the latest snapshot has any ``tasks[].interrupts``
entries. We emit one record per paused thread with the parsed
``snapshot.created_at`` so consumers can compute staleness.

Dev environments using ``MemorySaver`` (no ``alist`` iteration target)
are detected via ``isinstance`` and the sweep is skipped with an info
log — there is nothing durable to inspect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from agents.observability import get_logger, langgraph_paused_threads

slog = get_logger("paused_threads")

# Cap on how many paused threads to log on startup before we stop
# producing per-thread warnings. The count is always reported in the
# summary line; this just prevents a Loki log explosion if the cluster
# is genuinely full of stale paused threads (which would itself be a
# bug worth fixing manually).
DEFAULT_LOG_CAP = 100

# Default staleness threshold: a thread paused for more than this is
# flagged as "stale" on startup. 5 minutes is short enough that any
# real-life approval that the pod restarted away from will surface,
# long enough that a thread that was just paused (mid-restart) doesn't
# get flagged.
DEFAULT_STALE_AFTER_SECONDS = 300


def _update_gauge(threads: list[dict[str, Any]]) -> None:
    """Update the ``langgraph_paused_threads`` Gauge from a sweep result.

    Sets both label values on every call so the metric reflects the
    current truth, not a monotonic max. Called once per sweep —
    callers don't need to invoke this directly.
    """
    stale_count = sum(1 for t in threads if t["is_stale"])
    langgraph_paused_threads.labels(is_stale="true").set(stale_count)
    langgraph_paused_threads.labels(is_stale="false").set(
        len(threads) - stale_count
    )


async def _parse_created_at(value: str | None) -> datetime | None:
    """Best-effort parse of ``StateSnapshot.created_at``.

    Checkpointer emits ISO-8601 strings; defensive against None /
    malformed entries so a single bad row never aborts the sweep.
    """
    if not value:
        return None
    try:
        # fromisoformat handles "2024-01-01T12:34:56.789012+00:00" in 3.11+.
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def sweep_paused_threads(
    graph: Any,
    *,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return the list of currently-paused threads.

    Each entry is a dict shaped::

        {
            "thread_id": str,
            "paused_since": str | None,   # ISO-8601, from snapshot.created_at
            "paused_for_seconds": float | None,
            "is_stale": bool,             # paused_for_seconds > stale_after_seconds
            "interrupts": [
                {"id": str, "value": dict | None},
                ...
            ],
            "next": [str, ...],            # snapshot.next (nodes to run on resume)
        }

    When the checkpointer is in-memory (dev path), returns an empty
    list — there is no durable inventory to walk.
    """
    checkpointer = getattr(graph, "checkpointer", None)
    if checkpointer is None or isinstance(checkpointer, MemorySaver):
        slog.info("paused_threads_sweep_skipped", reason="in_memory_checkpointer")
        _update_gauge([])
        return []

    now = datetime.now(UTC)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    async for cp in checkpointer.alist({}):
        thread_id = cp.config.get("configurable", {}).get("thread_id")
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)

        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        interrupts_raw = [
            i
            for t in snapshot.tasks
            for i in t.interrupts
        ]
        if not interrupts_raw:
            continue

        created_at = await _parse_created_at(snapshot.created_at)
        paused_for = (
            (now - created_at).total_seconds() if created_at is not None else None
        )

        out.append(
            {
                "thread_id": thread_id,
                "paused_since": snapshot.created_at,
                "paused_for_seconds": paused_for,
                "is_stale": (
                    paused_for is not None and paused_for > stale_after_seconds
                ),
                "interrupts": [
                    {
                        "id": getattr(i, "id", None),
                        "value": (
                            dict(i.value)
                            if isinstance(getattr(i, "value", None), dict)
                            else None
                        ),
                    }
                    for i in interrupts_raw
                ],
                "next": list(snapshot.next),
            }
        )

        if limit is not None and len(out) >= limit:
            break

    _update_gauge(out)
    return out


DEFAULT_STARTUP_SWEEP_LIMIT = 200


async def startup_log_paused_threads(
    graph: Any,
    *,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    log_cap: int = DEFAULT_LOG_CAP,
    limit: int | None = DEFAULT_STARTUP_SWEEP_LIMIT,
) -> None:
    """Run the sweep on startup and log a structured warning per stale thread.

    Intended to be called from ``lifespan`` after the graph is compiled.
    Failures (Postgres unreachable mid-startup, transient query error)
    are caught and logged at warning level — we do **not** want a sweep
    failure to abort pod startup, because the sweep is purely
    diagnostic.

    TODO(P1.2 follow-up): when class-C/D nodes actually call
    ``interrupt()``, add an opt-in auto-resume step here that calls
    ``graph.ainvoke(None, config={...})`` per stale thread. Gate on a
    new setting (``startup_resume_paused_threads: bool = False``) so the
    default behavior stays read-only.
    """
    try:
        rows = await sweep_paused_threads(
            graph, stale_after_seconds=stale_after_seconds, limit=limit
        )
    except Exception as exc:
        slog.warning(
            "paused_threads_sweep_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return

    stale = [r for r in rows if r["is_stale"]]
    slog.info(
        "paused_threads_sweep_done",
        total_paused=len(rows),
        stale_count=len(stale),
        stale_after_seconds=stale_after_seconds,
    )

    # Per-thread warning lines, capped so a genuinely-stuck cluster
    # doesn't dump thousands of lines into Loki on every restart.
    for row in stale[:log_cap]:
        slog.warning(
            "paused_thread_stale",
            thread_id=row["thread_id"],
            paused_since=row["paused_since"],
            paused_for_seconds=row["paused_for_seconds"],
            next_nodes=row["next"],
            interrupt_count=len(row["interrupts"]),
        )
    if len(stale) > log_cap:
        slog.warning(
            "paused_thread_log_cap_hit",
            cap=log_cap,
            truncated=len(stale) - log_cap,
        )


__all__ = [
    "DEFAULT_LOG_CAP",
    "DEFAULT_STALE_AFTER_SECONDS",
    "startup_log_paused_threads",
    "sweep_paused_threads",
]
