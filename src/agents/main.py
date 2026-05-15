"""FastAPI app entrypoint.

The graph is built once at startup and stored on `app.state.graph`. In
production, the Postgres checkpointer persists state across pod restarts.
In dev (no Postgres available), the app falls back to an in-memory
checkpointer.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from agents.api import admin, approval, chat_completions, health, inbox
from agents.graphs.fleet import build_fleet_graph
from agents.settings import get_settings

logger = logging.getLogger("agents")


async def _build_checkpointer() -> object:
    """Return a checkpointer suitable for the runtime environment.

    Production: Postgres. Dev (no Postgres URL set to a reachable host):
    in-memory.
    """
    settings = get_settings()
    if settings.postgres_url.startswith("postgresql://localhost"):
        # Local-dev convenience: use in-memory unless we explicitly know
        # Postgres is up. Avoids "I want to smoke-test against ollama and
        # don't have a DB running" friction.
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            saver = AsyncPostgresSaver.from_conn_string(settings.postgres_url)
            await saver.setup()
            logger.info("using Postgres checkpointer at %s", settings.postgres_url)
            return saver
        except Exception as exc:  # noqa: BLE001
            logger.warning("Postgres checkpointer unavailable (%s); using MemorySaver", exc)
            from langgraph.checkpoint.memory import MemorySaver

            return MemorySaver()
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = AsyncPostgresSaver.from_conn_string(settings.postgres_url)
        await saver.setup()
        return saver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logger.info("starting langgraph-agents, vault_root=%s", settings.vault_root)

    checkpointer = await _build_checkpointer()
    app.state.graph = build_fleet_graph(checkpointer=checkpointer)
    logger.info("fleet graph compiled; entrypoint=triager")

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


def run() -> None:
    """Entrypoint for `python -m agents` and the project script."""
    import uvicorn

    uvicorn.run("agents.main:app", host="0.0.0.0", port=8765, log_level="info")  # noqa: S104


if __name__ == "__main__":
    run()
