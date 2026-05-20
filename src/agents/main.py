"""FastAPI app entrypoint.

The graph is built once at startup and stored on `app.state.graph`. In
production, the Postgres checkpointer persists state across pod restarts.
In dev (no Postgres available), the app falls back to an in-memory
checkpointer.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Response
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool

from agents.api import admin, approval, chat_completions, health, inbox
from agents.graphs.fleet import build_fleet_graph
from agents.idempotency import DedupStore
from agents.memory_store import MCPMemoryStore, build_pool
from agents.observability import (
    configure_structlog,
    flush_langfuse,
    init_langfuse,
    init_otel,
    instrument_fastapi_app,
    metrics_text,
)
from agents.paused_threads import startup_log_paused_threads
from agents.queue import TaskQueue
from agents.queue.migrate import ensure_schema as ensure_queue_schema
from agents.queue.worker import QueueWorker
from agents.settings import Settings, get_settings
from agents.state import (
    ActivityLogEntry,
    ApprovalRequest,
    FleetState,
    RejectionSignal,
    TriageDecision,
)

# Pydantic models that ride inside FleetState across checkpoint boundaries.
# langgraph-checkpoint 4.x serde issues a deprecation warning ("Deserializing
# unregistered type ...") for any non-allowlisted module, and the message says
# future versions will block them outright. Registering here clears the
# warning today and prevents a future-version footgun. Each tuple is
# (module, classname) — matches the format the warning prints.
_AGENTS_STATE_TYPES = [
    FleetState,
    TriageDecision,
    RejectionSignal,
    ApprovalRequest,
    ActivityLogEntry,
]

logger = logging.getLogger("agents")


async def _build_checkpointer(stack: AsyncExitStack) -> BaseCheckpointSaver[Any]:
    """Return a checkpointer suitable for the runtime environment.

    Production: Postgres via `AsyncConnectionPool`. Dev (no reachable
    Postgres): in-memory.

    The pool is entered via the AsyncExitStack so it's cleaned up on
    shutdown. The pool — NOT a single `AsyncConnection` from
    `from_conn_string` — is the supported path here: psycopg's pool
    health-checks connections before yielding them, transparently
    replacing any that have gone half-open (e.g. silently dropped by
    Cilium conntrack or NAT idle timeouts during long pod idle
    periods). The single-connection variant hangs forever on first-use
    after a long idle window — see the v0.2.11 reporter-route hang
    forensics (idle conn aged 88min on cluster postgres before the
    next aget_tuple parked on the dead socket).

    `autocommit=True` + `prepare_threshold=0` mirror the kwargs
    langgraph-checkpoint-postgres uses internally for its own
    connections; the saver manages transactions itself and uses its
    own prepared-statement registry.
    """
    settings = get_settings()

    async def _try_postgres() -> BaseCheckpointSaver[Any]:
        pool = AsyncConnectionPool(
            conninfo=settings.postgres_url,
            min_size=1,
            max_size=10,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                # Belt + suspenders for silently-half-open TCP. The
                # pool's `check=` (SELECT 1) handles HARD closes
                # (server-side pg_terminate_backend) cleanly: postgres
                # sends a close-conn message, psycopg raises, the pool
                # discards. SILENT half-opens (Cilium conntrack expiry
                # between cluster pods, NAT idle drops) don't send
                # anything — the socket stays kernel-ESTABLISHED on
                # both sides while being functionally dead. SELECT 1
                # on that fd writes bytes that vanish; the recv parks
                # forever waiting for an ACK that never comes; `check=`
                # parks with it. The cluster exhibits exactly this
                # after ~2min idle between /inbox requests — keepalives
                # alone aren't enough because they fire only after
                # `keepalives_idle` seconds of true idle, and SELECT 1
                # mid-flight isn't idle.
                #
                # `tcp_user_timeout` (Linux, ms) is the right primitive:
                # it tells the kernel to give up on a connection if
                # outgoing data has been unacknowledged for this long
                # — independent of keepalive cadence. With 15s, a dead
                # conn's SELECT 1 raises within 15s instead of hanging
                # forever. Keepalives stay on as defense-in-depth for
                # detecting dead conns during long-actually-idle windows
                # (e.g. between checkpoints during a slow LLM call).
                # See `project_langgraph_reporter_post_node_hang`.
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "tcp_user_timeout": 15000,
            },
            check=AsyncConnectionPool.check_connection,
            # max_idle: preemptively recycle conns that have been idle
            # in the pool for >60s. tcp_user_timeout protects against
            # silent half-open mid-op, but recycling idle conns BEFORE
            # they have a chance to go stale is the simpler primitive:
            # Cilium conntrack expiry typically lands >60s, so a freshly
            # recycled conn is virtually never stale at yield time.
            max_idle=60,
            open=False,
        )
        await stack.enter_async_context(pool)
        saver = AsyncPostgresSaver(
            conn=pool,  # type: ignore[arg-type]
            serde=JsonPlusSerializer(
                allowed_msgpack_modules=_AGENTS_STATE_TYPES,
            ),
        )
        await saver.setup()
        return saver

    if settings.postgres_url.startswith("postgresql://localhost"):
        # Local-dev convenience: try Postgres but fall back to memory if it's
        # not actually running.
        try:
            saver = await _try_postgres()
            logger.info("using Postgres checkpointer at %s", settings.postgres_url)
            return saver
        except Exception as exc:
            logger.warning("Postgres checkpointer unavailable (%s); using MemorySaver", exc)
            return MemorySaver()

    return await _try_postgres()


def _checkpointer_pool(checkpointer: BaseCheckpointSaver[Any]) -> AsyncConnectionPool | None:
    """Return the AsyncConnectionPool the checkpointer is using, or None.

    The AsyncPostgresSaver instance carries the pool as `.conn`. In the
    MemorySaver dev path there is no pool — return None and the queue
    worker stays disabled (the dev path never enqueues real work).
    """
    conn = getattr(checkpointer, "conn", None)
    if conn is None or not hasattr(conn, "connection"):
        return None
    return cast("AsyncConnectionPool[Any]", conn)


async def _build_store(stack: AsyncExitStack, settings: Settings) -> MCPMemoryStore | None:
    """Build the long-term cross-agent KG store, or None to disable.

    Disabled (returns None) when `memory_backend=none` or when the
    memory Postgres is at localhost and not actually reachable (dev
    convenience). The fleet graph compiles cleanly with `store=None` —
    agents just don't get long-term store access.
    """
    if settings.memory_backend != "postgres":
        logger.info("memory_backend=%s; long-term store disabled", settings.memory_backend)
        return None

    async def _try_open() -> MCPMemoryStore:
        pool = await build_pool(settings.memory_postgres_url)
        stack.push_async_callback(pool.close)
        return MCPMemoryStore(
            pool=pool,
            # Use the dedicated memory Ollama URL if set, else fall back
            # to the P40 endpoint. Pointing at ollama-spark avoids the
            # P40 model-swap churn (bge-m3 + qwen2.5:7b can't both live
            # in P40's MAX_LOADED=1 slot).
            ollama_base_url=settings.memory_ollama_url or settings.ollama_p40_url,
            embed_model=settings.memory_embed_model,
        )

    if settings.memory_postgres_url.startswith("postgresql://localhost"):
        try:
            store = await _try_open()
            logger.info("MCPMemoryStore connected (dev/localhost)")
            return store
        except Exception as exc:
            logger.warning("MCPMemoryStore unavailable (%s); long-term store disabled", exc)
            return None

    store = await _try_open()
    logger.info("MCPMemoryStore connected (kg.*) — shared with memory-mcp Phase 0")
    return store


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    # JSON structured logs → stdout → Vector → Loki (Path A per plan v5).
    configure_structlog(level=settings.log_level)
    # Langfuse per-task trace UI. Skipped silently if LANGFUSE_HOST /
    # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY aren't set yet — the
    # langgraph-agents secret already carries placeholders that the
    # operator fills in once a project is created in the langfuse UI.
    init_langfuse()
    # Phase 3.K — OTel tracer + httpx auto-instrumentation. Silent
    # disable if collector unreachable. FastAPI app instrumentation
    # lives at module scope below so the wrapping happens before the
    # first request.
    init_otel()
    logger.info("starting langgraph-agents, vault_root=%s", settings.vault_root)

    async with AsyncExitStack() as stack:
        checkpointer = await _build_checkpointer(stack)
        store = await _build_store(stack, settings)
        app.state.graph = build_fleet_graph(checkpointer=checkpointer, store=store)
        # Phase 3.G: idempotency dedup store backed by Dragonfly.
        # Constructed lazily — first connect happens on first /inbox
        # request that carries an `idempotency_key`. Closed on shutdown
        # via the exit-stack callback.
        app.state.dedup_store = DedupStore(
            url=settings.dragonfly_url,
            default_ttl_seconds=settings.idempotency_ttl_seconds,
        )
        stack.push_async_callback(app.state.dedup_store.close)

        # Phase 4.M2 — task queue substrate. Reuses the checkpointer's
        # Postgres pool (same CNPG cluster, same Barman backup line).
        # `ensure_schema` applies idempotent migrations on startup. The
        # `QueueWorker` runs as a background asyncio task draining the
        # queue and invoking the graph (the work the synchronous /inbox
        # used to do inline).
        queue_pool = _checkpointer_pool(checkpointer)
        app.state.queue_pool = queue_pool
        if queue_pool is not None:
            await ensure_queue_schema(queue_pool)
            app.state.task_queue = TaskQueue(queue_pool)
            worker = QueueWorker(
                queue=app.state.task_queue,
                pool=queue_pool,
                graph=app.state.graph,
            )
            worker_task = asyncio.create_task(worker.run())
            app.state.queue_worker_task = worker_task
            stack.push_async_callback(worker.stop)
            logger.info("queue worker started")
        else:
            app.state.task_queue = None
            app.state.queue_worker_task = None
            logger.warning("queue worker NOT started (no Postgres pool — dev/MemorySaver path)")

        logger.info(
            "fleet graph compiled; entrypoint=triager; store=%s",
            "MCPMemoryStore" if store is not None else "disabled",
        )

        # P3.7: diagnostic sweep — log any threads paused at interrupt()
        # that survived the pod restart. Default OFF.
        #
        # The sweep walks the langgraph_checkpoints table and calls
        # `aget_state` per unique thread. On an instance with days of
        # history this STARVES the Postgres connection pool — even as
        # a background task, the per-thread connection it holds blocks
        # /inbox from getting a checkpoint write through. (Verified
        # empirically against v0.2.22: /admin/asyncio-tasks showed
        # sweep stuck at `aget_tuple → __aenter__` while inbox
        # requests timed out waiting for a pool slot.)
        #
        # Opt in via `STARTUP_SWEEP_ENABLED=true` once class-C/D nodes
        # routinely produce paused threads worth resuming. The
        # on-demand `/admin/paused-threads` endpoint runs the sweep
        # regardless of this flag.
        sweep_task: asyncio.Task[None] | None = None
        if settings.startup_sweep_enabled:
            sweep_task = asyncio.create_task(startup_log_paused_threads(app.state.graph))
            app.state.startup_sweep_task = sweep_task
        else:
            logger.info(
                "startup paused-threads sweep disabled (set STARTUP_SWEEP_ENABLED=true to enable)"
            )

        yield
        logger.info("shutting down")
        if sweep_task is not None and not sweep_task.done():
            sweep_task.cancel()
        worker_task = app.state.queue_worker_task
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
        flush_langfuse()


app = FastAPI(
    title="langgraph-agents",
    version="0.1.0",
    description="Multi-agent fleet runtime — LangGraph + ollama + Claude API",
    lifespan=lifespan,
)

# Phase 3.K — FastAPI request auto-instrumentation. No-op when OTel
# init failed in lifespan (provider is the NoOp default). Applied here
# rather than inside `lifespan` so it wraps the routes before any
# request lands.
instrument_fastapi_app(app)

app.include_router(health.router)
app.include_router(inbox.router)
app.include_router(approval.router)
app.include_router(admin.router)
app.include_router(chat_completions.router)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus scrape endpoint. ServiceMonitor in `kubernetes/apps/ai/
    langgraph-agents/app/` targets this path on port 8765.
    """
    payload, content_type = metrics_text()
    return Response(content=payload, media_type=content_type)


def run() -> None:
    """Entrypoint for `python -m agents` and the project script."""
    uvicorn.run("agents.main:app", host="0.0.0.0", port=8765, log_level="info")


if __name__ == "__main__":
    run()
