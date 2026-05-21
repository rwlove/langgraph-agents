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


def _split_statements(sql: str) -> list[str]:
    """Split SQL on top-level semicolons.

    The checkpointer pool is constructed with ``prepare_threshold=0``
    (psycopg's "prepare every statement" mode) and that bites here:
    psycopg's extended-query protocol can't carry multi-statement SQL,
    so a CREATE TABLE + CREATE INDEX file passed wholesale to
    ``conn.execute`` raises ``cannot insert multiple commands into a
    prepared statement``. Splitting and dispatching one statement at a
    time sidesteps it. Only top-level ``;`` count: lines starting with
    ``--`` are skipped to avoid treating a semicolon inside a comment
    as a separator. We don't try to handle ``;`` inside string literals
    or dollar-quoted blocks — the queue migrations are plain DDL and
    don't contain either.
    """
    statements: list[str] = []
    buf: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        # Strip pure-comment lines so a `;` inside a comment can't
        # close a statement. Inline trailing `-- ...` is fine; psycopg
        # passes it through.
        if line.lstrip().startswith("--"):
            continue
        buf.append(line)
        if line.endswith(";"):
            joined = "\n".join(buf).strip().rstrip(";").strip()
            if joined:
                statements.append(joined)
            buf = []
    # Trailing statement without a terminating ;
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


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
        statements = _split_statements(sql)
        logger.info("applying migration: %s (%d statements)", f.name, len(statements))
        async with pool.connection() as conn:
            for stmt in statements:
                await conn.execute(stmt)
    logger.info("task-queue schema ready")
