"""End-to-end regression for the AsyncConnectionPool-backed checkpointer.

Why this test exists: v0.2.x running on `AsyncPostgresSaver.from_conn_string`
(single non-pooled `AsyncConnection`) hung indefinitely on first-use after
a long idle window — the conn was silently half-open (Cilium conntrack /
NAT idle timeout) and psycopg has no health-check on `from_conn_string`'s
connection. Forensics on 2026-05-19: a pod-startup conn idle for 88min
parked the next `aget_tuple` forever. The pool path
(`AsyncPostgresSaver(conn=AsyncConnectionPool(...))`) health-checks
connections before yielding and transparently replaces dead conns.

Two scenarios:
1. happy path — graph runs end-to-end against postgres, 4 checkpoint
   rows land for the thread, graph reaches END.
2. recovery path — after a server-side `pg_terminate_backend` of every
   open conn for the test user, a second graph invocation still
   completes. This is the regression gate: with the old single-conn
   path, step 2 hangs forever.

Run target:
- CI: a postgres service container provides `POSTGRES_TEST_URL`.
- Local: skip unless `POSTGRES_TEST_URL` is exported. No docker / no
  testcontainers dep added to the project to keep dev-loop cheap.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agents.graphs.fleet import build_fleet_graph
from agents.nodes import NODES
from agents.state import FleetState, TriageDecision

POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL not set; integration test requires real postgres",
)


def _fake_triager_returning(target: str) -> Callable[[FleetState], dict[str, Any]]:
    def _node(state: FleetState) -> dict[str, Any]:
        decision = TriageDecision(
            summary="fake",
            domain="homelab",
            intent="question",
            target_agent=target,  # type: ignore[arg-type]
            confidence=0.95,
            reasoning="fake",
        )
        return {"triage": decision, "target_agent": target}
    return _node


def _fake_reporter() -> Callable[[FleetState], dict[str, Any]]:
    def _node(state: FleetState) -> dict[str, Any]:
        return {"output": "fake reporter output"}
    return _node


@pytest_asyncio.fixture
async def reset_db() -> AsyncIterator[None]:
    """Wipe checkpoint tables before each test so thread_id counts are clean."""
    async with await psycopg.AsyncConnection.connect(POSTGRES_TEST_URL) as conn:  # type: ignore[arg-type]
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            # setup() will recreate; drop is fine. Wrapped in EXISTS so a
            # fresh DB doesn't error on the first run.
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints",
                          "checkpoint_migrations"):
                await cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    yield


@pytest_asyncio.fixture
async def pool_saver() -> AsyncIterator[tuple[AsyncConnectionPool, AsyncPostgresSaver]]:
    """Same construction the production app uses (see agents.main._build_checkpointer)."""
    pool = AsyncConnectionPool(
        conninfo=POSTGRES_TEST_URL,  # type: ignore[arg-type]
        min_size=1,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(conn=pool)  # type: ignore[arg-type]
        await saver.setup()
        yield pool, saver
    finally:
        await pool.close()


async def _count_checkpoints(thread_id: str) -> int:
    async with await psycopg.AsyncConnection.connect(POSTGRES_TEST_URL) as conn:  # type: ignore[arg-type]
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def _terminate_all_user_conns(database_url: str) -> int:
    """Server-side terminate every backend connection for this DB user.

    Simulates the silent conn drop the cluster exhibits (Cilium conntrack
    expiry). Returns the count of terminated backends.
    """
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND usename = current_user"
            )
            rows = await cur.fetchall()
            return len(rows)


async def _run_graph(
    pool_saver: tuple[AsyncConnectionPool, AsyncPostgresSaver],
    task_id: str,
    temp_vault: Path,
) -> Any:
    _, saver = pool_saver
    with patch.dict(
        NODES,
        {
            "triager": _fake_triager_returning("reporter"),
            "reporter": _fake_reporter(),
        },
    ):
        graph = build_fleet_graph(checkpointer=saver)
        initial = FleetState(
            task_id=task_id,
            source="test",
            content="anything",
        )
        config: dict[str, Any] = {"configurable": {"thread_id": task_id}}
        return await graph.ainvoke(initial, config=config)


async def test_pool_completes_graph_run(
    reset_db: None,
    pool_saver: tuple[AsyncConnectionPool, AsyncPostgresSaver],
    temp_vault: Path,
) -> None:
    """Happy path: graph runs end-to-end + post-reporter checkpoint lands."""
    task_id = "pool-happy"
    final = await _run_graph(pool_saver, task_id, temp_vault)

    assert final.get("output") == "fake reporter output"
    # START → triager → reporter → END = 4 checkpoint rows
    count = await _count_checkpoints(task_id)
    assert count == 4, f"expected 4 checkpoints, got {count}"


async def test_pool_recovers_after_server_terminates_conns(
    reset_db: None,
    pool_saver: tuple[AsyncConnectionPool, AsyncPostgresSaver],
    temp_vault: Path,
) -> None:
    """Regression gate for the v0.2.11 hang.

    1. Run graph once (warms the pool, populates conn).
    2. Server-side terminate every backend conn — simulates the silent
       conntrack expiry that the cluster hit.
    3. Run graph again. Pool's check-on-yield must replace the dead
       conn; second invocation must complete within a reasonable
       deadline (10s here — way under the 88min cluster hang).

    With single-conn `from_conn_string`, step 3 parks forever on the
    next aget_tuple.
    """
    # Warm up
    await _run_graph(pool_saver, "pool-warm", temp_vault)
    assert await _count_checkpoints("pool-warm") == 4

    # Kill every backend for this DB user
    terminated = await _terminate_all_user_conns(POSTGRES_TEST_URL)  # type: ignore[arg-type]
    assert terminated >= 1, "expected at least one terminated conn"

    # Second run — must NOT hang. 10s is generous; if the pool isn't
    # replacing dead conns this will time out and fail loudly.
    async with asyncio.timeout(10):
        await _run_graph(pool_saver, "pool-recover", temp_vault)
    assert await _count_checkpoints("pool-recover") == 4
