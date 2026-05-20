"""FastAPI app entrypoint.

The graph is built once at startup and stored on `app.state.graph`. In
production, the Postgres checkpointer persists state across pod restarts.
In dev (no Postgres available), the app falls back to an in-memory
checkpointer.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Response
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool

from agents.api import admin, approval, chat_completions, health, inbox
from agents.graphs.fleet import build_fleet_graph
from agents.memory_store import MCPMemoryStore, build_pool
from agents.observability import configure_structlog, metrics_text
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
            kwargs={"autocommit": True, "prepare_threshold": 0},
            # `check` runs SELECT 1 before yielding a conn — a dead conn
            # (silent conntrack expiry, server-side terminate, NAT idle
            # drop) fails the check and gets replaced, instead of being
            # handed out and parking the next aget_tuple. This is the
            # behavior the v0.2.11 hang needed.
            check=AsyncConnectionPool.check_connection,
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


async def _build_store(
    stack: AsyncExitStack, settings: Settings
) -> MCPMemoryStore | None:
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
            ollama_base_url=settings.ollama_p40_url,
            embed_model=settings.memory_embed_model,
        )

    if settings.memory_postgres_url.startswith("postgresql://localhost"):
        try:
            store = await _try_open()
            logger.info("MCPMemoryStore connected (dev/localhost)")
            return store
        except Exception as exc:
            logger.warning(
                "MCPMemoryStore unavailable (%s); long-term store disabled", exc
            )
            return None

    store = await _try_open()
    logger.info(
        "MCPMemoryStore connected (kg.*) — shared with memory-mcp Phase 0"
    )
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
    logger.info("starting langgraph-agents, vault_root=%s", settings.vault_root)

    async with AsyncExitStack() as stack:
        checkpointer = await _build_checkpointer(stack)
        store = await _build_store(stack, settings)
        app.state.graph = build_fleet_graph(
            checkpointer=checkpointer, store=store
        )
        logger.info(
            "fleet graph compiled; entrypoint=triager; store=%s",
            "MCPMemoryStore" if store is not None else "disabled",
        )
        yield
        logger.info("shutting down")


app = FastAPI(
    title="langgraph-agents",
    version="0.1.0",
    description="Multi-agent fleet runtime — LangGraph + ollama + Claude API",
    lifespan=lifespan,
)

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
