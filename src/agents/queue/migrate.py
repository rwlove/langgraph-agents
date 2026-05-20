"""Idempotent schema migrations for the task queue.

Per Phase 4.M1. Applied at app startup by `agents.main.lifespan` after
the Postgres pool is established. The SQL files in `migrations/` use
`CREATE TABLE IF NOT EXISTS` so re-applying is a no-op.
"""

from __future__ import annotations

import logging
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("agents.queue.migrate")


async def ensure_schema(pool: AsyncConnectionPool) -> None:
    """Apply all migrations in order. Safe to call on every startup."""
    files = sorted(
        resources.files("agents.queue.migrations").iterdir(),
        key=lambda f: f.name,
    )
    for f in files:
        if not f.name.endswith(".sql"):
            continue
        sql = f.read_text(encoding="utf-8")
        logger.info("applying migration: %s", f.name)
        async with pool.connection() as conn:
            await conn.execute(sql)
    logger.info("task-queue schema ready")
