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

from agents.api import admin, approval, chat_completions, health, inbox
from agents.graphs.fleet import build_fleet_graph
from agents.memory_store import MCPMemoryStore, build_pool
from agents.observability import configure_structlog, metrics_text
from agents.settings import Settings, get_settings

logger = logging.getLogger("agents")


async def _build_checkpointer(stack: AsyncExitStack) -> BaseCheckpointSaver[Any]:
    """Return a checkpointer suitable for the runtime environment.

    Production: Postgres. Dev (no reachable Postgres): in-memory.

    The Postgres saver is an async context manager; entering it via the
    AsyncExitStack ensures the connection pool is cleaned up on shutdown.
    """
    settings = get_settings()

    async def _try_postgres() -> BaseCheckpointSaver[Any]:
        cm = AsyncPostgresSaver.from_conn_string(settings.postgres_url)
        saver = await stack.enter_async_context(cm)
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
