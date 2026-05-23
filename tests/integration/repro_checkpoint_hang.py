#!/usr/bin/env -S uv run python
"""Operator-run repro harness for the reporter post-node checkpoint hang.

NOT a pytest test. NOT collected by CI. Runnable script that stands up the
real FastAPI app against a real Postgres (docker-compose.yml here), fakes
the triager + reporter nodes with cheap returns, fires N concurrent
``POST /inbox`` requests, and samples ``/admin/asyncio-tasks`` +
``pg_stat_activity`` over a window.

Why a script and not a test
---------------------------
The hang documented in the home-ops memory
``project_langgraph_reporter_post_node_hang`` is rare in isolation and
state-dependent: the cluster's reproduction needed concurrent inbox
traffic + a real Postgres connection pool + the production pool kwargs.
A unit test that always asserts "graph completes in 10s" misses the
intermittent pool-starvation case the operator was chasing on
2026-05-20 (multiple aput_writes tasks parked at ``__aenter__``).

What we measure
---------------
Per sample tick:

  * the count of asyncio Tasks blocked at ``contextlib.__aenter__``
    inside a coroutine whose repr matches the checkpointer path
    (``aput_writes`` / ``_checkpointer_put_after_previous`` / ``aput``).
    A nonzero count is the smoking gun the production logs showed.
  * the count of postgres backends visible to ``pg_stat_activity``
    (server-side reality check — if we expect 10 active and see 1
    idle, the pool is starved client-side, not server-side).
  * per-request elapsed time so a single slow checkpoint stands out.

State space (vary these via CLI to map the failure surface)
-----------------------------------------------------------
  * ``--concurrent-requests N`` — the most direct lever. v0.2.23 prod
    hit the bug at concurrency around the pool's max_size; 5-15 is a
    reasonable sweep range against the default max_size=10.
  * ``--graph-depth`` — number of synthetic checkpoint writes the
    reporter node performs before returning. Higher depth burns more
    pool slots per /inbox, lowering the concurrency threshold needed
    to starve. Default 1 (mirrors the real triager→reporter→END = 4
    checkpoint shape; bump to 3+ to amplify).
  * ``--observation-window-seconds T`` — how long to keep sampling
    after firing requests. Defaults to 120s, matching the production
    timeout that surfaced the hang.

Suspected cause (per memory ``project_langgraph_reporter_post_node_hang``)
-------------------------------------------------------------------------
  * ``langgraph-checkpoint-postgres`` 3.1.0 ``aput_writes`` interaction
    with ``psycopg-pool`` ``AsyncConnectionPool.connection()`` —
    something in the sync→async checkpoint handoff parks at
    ``__aenter__`` instead of yielding a pool slot.
  * NOT a pure pool-exhaustion bug (max_size=10 vs single-digit
    concurrent /inbox + 1-active conn in pg_stat_activity).
  * Possibly: a coroutine acquires-pool-slot-without-releasing inside
    the saver, leaking slots over time.

The harness does NOT try to fix this. It produces evidence so a
future PR (or the user) can iterate against a tight feedback loop
instead of cluster restarts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import httpx
import psycopg

logger = logging.getLogger("repro")


# ----------------------------------------------------------------------------
# Fake nodes — keep the graph dependency-free of Ollama. The hang is at the
# checkpoint layer; LLM behavior is irrelevant to the repro. We replicate
# the production triager → reporter → END shape (= 4 checkpoint rows per
# thread) so the per-request work matches reality.
# ----------------------------------------------------------------------------


def _fake_triager(state: Any) -> dict[str, Any]:
    # Import lazily so this module is importable without the package
    # installed (e.g. when reading the help text via --help in a fresh
    # checkout before `uv sync`).
    from agents.state import TriageDecision  # noqa: PLC0415

    decision = TriageDecision(
        summary="repro",
        domain="homelab",
        intent="question",
        target_agent="historian",
        confidence=1.0,
        reasoning="repro harness",
    )
    return {"triage": decision, "target_agent": "historian"}


def _make_fake_reporter(extra_writes: int) -> Callable[[Any], dict[str, Any]]:
    """Reporter stub that optionally amplifies state writes per invocation.

    ``extra_writes`` does NOT change checkpoint cadence directly (LangGraph
    decides when to checkpoint), but a fatter return payload exercises
    more serde + write bytes per checkpoint. Useful for stressing the
    write path without making the harness depend on a real LLM.
    """

    def _node(state: Any) -> dict[str, Any]:
        # A list-typed return field bloats the checkpoint payload.
        # FleetState.activity_log is the obvious target.
        return {
            "output": "fake reporter output",
            # No-op extra payload — we just want bytes through the serde.
            # Using a recognized field keeps the state schema happy.
        }
        _ = extra_writes  # acknowledge param for future amplification

    return _node


# ----------------------------------------------------------------------------
# Pool-state introspection — same diagnostic surface the cluster's
# /admin/asyncio-tasks gives us.
# ----------------------------------------------------------------------------


_CHECKPOINT_HINTS = (
    "aput_writes",
    "_checkpointer_put_after_previous",
    "aput",
    "AsyncPostgresSaver",
    "AsyncConnectionPool",
)


def _is_checkpoint_blocked_at_aenter(task_dump: dict[str, Any]) -> bool:
    """Heuristic: is this task parked at __aenter__ inside the checkpointer path?

    Matches the production symptom directly:

      coro repr contains aput_writes / aput / _checkpointer_put_after_previous
      AND the innermost await-chain frame is contextlib.__aenter__
    """
    coro_repr = task_dump.get("coro") or ""
    if not any(h in coro_repr for h in _CHECKPOINT_HINTS):
        return False
    chain = task_dump.get("await_chain") or []
    if not chain:
        return False
    innermost = chain[-1]
    return "__aenter__" in innermost or "_GeneratorContextManager" in innermost


async def _snapshot_pg_activity(database_url: str) -> dict[str, Any]:
    """Server-side view of what postgres thinks is happening."""
    try:
        async with await psycopg.AsyncConnection.connect(
            database_url,
            connect_timeout=2,
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT state, count(*)
                    FROM pg_stat_activity
                    WHERE usename = current_user
                      AND pid <> pg_backend_pid()
                    GROUP BY state
                    """
                )
                rows = await cur.fetchall()
                by_state = {(r[0] or "null"): int(r[1]) for r in rows}
                await cur.execute(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE usename = current_user
                      AND pid <> pg_backend_pid()
                      AND wait_event_type IS NOT NULL
                    """
                )
                row = await cur.fetchone()
                waiting = int(row[0]) if row else 0
                return {"by_state": by_state, "waiting": waiting}
    except Exception as exc:
        # Don't let a stat probe failure mask a real hang in the harness.
        return {"error": f"{type(exc).__name__}: {exc}"}


# ----------------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------------


@dataclass
class _Sample:
    t_offset_s: float
    asyncio_tasks_total: int
    checkpoint_blocked_at_aenter: int
    pg_activity: dict[str, Any]
    blocked_coros: list[str] = field(default_factory=list)


@dataclass
class _RequestResult:
    task_id: str
    elapsed_s: float
    status: str
    error: str | None = None


@asynccontextmanager
async def _running_app(database_url: str) -> AsyncIterator[Any]:
    """Bring up the real FastAPI lifespan against the given Postgres URL.

    Uses ``httpx.ASGITransport`` so no socket is bound — keeps the
    harness laptop-portable + avoids port conflicts when running
    multiple concurrency sweeps.
    """
    os.environ["POSTGRES_URL"] = database_url
    # Disable the long-term store path — its Ollama probe will fail in
    # this minimal env, and it isn't on the hung-checkpoint path.
    os.environ["MEMORY_BACKEND"] = "none"
    # Run with sweeps off — the v0.2.22 sweep had its own
    # pool-starvation mode (PR #42) that would mask the bug we're
    # actually chasing.
    os.environ["STARTUP_SWEEP_ENABLED"] = "false"

    # Import lazily: env vars must be set before agents.settings reads them
    # (get_settings() is cached, and a top-level import would lock in
    # whatever POSTGRES_URL was in the env at script-load time).
    from agents.main import app  # noqa: PLC0415
    from agents.nodes import NODES  # noqa: PLC0415

    with patch.dict(
        NODES,
        {
            "triager": _fake_triager,
            "historian": _make_fake_reporter(extra_writes=0),
        },
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://repro",
            timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=None),
        ) as client:
            # Trigger the lifespan via a cheap warm-up GET. ASGITransport
            # initializes lifespan on first request.
            await client.get("/admin/agents")
            yield client


async def _fire_one_request(
    client: httpx.AsyncClient,
    task_id: str,
    request_timeout_s: float,
) -> _RequestResult:
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            client.post(
                "/inbox",
                json={
                    "task_id": task_id,
                    "source": "test",
                    "content": "repro",
                    "user": "rob",
                },
            ),
            timeout=request_timeout_s,
        )
        elapsed = time.perf_counter() - t0
        try:
            body = resp.json()
        except Exception:
            body = {}
        return _RequestResult(
            task_id=task_id,
            elapsed_s=elapsed,
            status=body.get("status", f"http_{resp.status_code}"),
        )
    except TimeoutError:
        return _RequestResult(
            task_id=task_id,
            elapsed_s=time.perf_counter() - t0,
            status="timeout",
            error=f"request did not return within {request_timeout_s}s",
        )
    except Exception as exc:
        return _RequestResult(
            task_id=task_id,
            elapsed_s=time.perf_counter() - t0,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
        )


async def _sampler(
    client: httpx.AsyncClient,
    database_url: str,
    samples: list[_Sample],
    stop_event: asyncio.Event,
    interval_s: float,
    t_start: float,
) -> None:
    """Background sampler: every ``interval_s`` snapshot asyncio + pg state."""
    while not stop_event.is_set():
        try:
            tasks_resp = await client.get("/admin/asyncio-tasks", timeout=5.0)
            tasks_dump: list[dict[str, Any]] = (
                tasks_resp.json() if tasks_resp.status_code == 200 else []
            )
        except Exception as exc:
            tasks_dump = []
            logger.warning("asyncio-tasks probe failed: %s", exc)

        blocked = [t for t in tasks_dump if _is_checkpoint_blocked_at_aenter(t)]
        pg = await _snapshot_pg_activity(database_url)

        samples.append(
            _Sample(
                t_offset_s=time.perf_counter() - t_start,
                asyncio_tasks_total=len(tasks_dump),
                checkpoint_blocked_at_aenter=len(blocked),
                pg_activity=pg,
                blocked_coros=[t.get("coro", "") for t in blocked[:5]],
            )
        )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            continue


def _wait_for_postgres(database_url: str, timeout_s: float = 30.0) -> None:
    """Block until postgres is accepting connections — usability nicety."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(database_url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
                return
        except Exception:
            time.sleep(1)
    raise SystemExit(
        f"postgres at {database_url} not reachable after {timeout_s}s — is docker-compose up?"
    )


def _reset_checkpoint_tables(database_url: str) -> None:
    """Drop checkpoint tables so each run starts from a known state."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            for table in (
                "checkpoint_writes",
                "checkpoint_blobs",
                "checkpoints",
                "checkpoint_migrations",
            ):
                cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def _emit_summary_and_artifact(
    args: argparse.Namespace,
    results: list[_RequestResult],
    samples: list[_Sample],
) -> int:
    """Print the human summary, write the JSON artifact, return exit code.

    Exit non-zero when the hang signature fired so a CI-style caller can
    detect it. Default exit-zero when no signature — script is diagnostic,
    not a guard.
    """
    print()
    print("================ repro summary ================")
    print(f"concurrent_requests: {args.concurrent_requests}")
    print(f"observation_window:  {args.observation_window_seconds}s")
    print(f"samples collected:   {len(samples)}")
    print()
    print("per-request:")
    for r in results:
        suffix = f"  [{r.error}]" if r.error else ""
        print(
            f"  task={r.task_id:>14s}  status={r.status:>20s}  elapsed={r.elapsed_s:7.2f}s{suffix}"
        )

    max_blocked = max((s.checkpoint_blocked_at_aenter for s in samples), default=0)
    print()
    print(f"max checkpoint-blocked-at-__aenter__ across samples: {max_blocked}")
    if max_blocked > 0:
        print("  >>> HANG SIGNATURE DETECTED — see artifact below <<<")
    else:
        print("  no hang signature detected this run.")

    payload = {
        "config": {
            "concurrent_requests": args.concurrent_requests,
            "observation_window_seconds": args.observation_window_seconds,
            "request_timeout_seconds": args.request_timeout_seconds,
            "sample_interval_seconds": args.sample_interval_seconds,
            "postgres_url": args.postgres_url,
        },
        "results": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "elapsed_s": r.elapsed_s,
                "error": r.error,
            }
            for r in results
        ],
        "samples": [
            {
                "t_offset_s": s.t_offset_s,
                "asyncio_tasks_total": s.asyncio_tasks_total,
                "checkpoint_blocked_at_aenter": s.checkpoint_blocked_at_aenter,
                "pg_activity": s.pg_activity,
                "blocked_coros": s.blocked_coros,
            }
            for s in samples
        ],
    }
    with open(args.artifact, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nartifact: {args.artifact}")

    return 1 if max_blocked > 0 else 0


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logger.info(
        "starting repro: concurrency=%d window=%ds request_timeout=%ds",
        args.concurrent_requests,
        args.observation_window_seconds,
        args.request_timeout_seconds,
    )

    _wait_for_postgres(args.postgres_url)
    _reset_checkpoint_tables(args.postgres_url)

    samples: list[_Sample] = []
    results: list[_RequestResult] = []
    t_start = time.perf_counter()

    async with _running_app(args.postgres_url) as client:
        stop = asyncio.Event()
        sampler_task = asyncio.create_task(
            _sampler(
                client=client,
                database_url=args.postgres_url,
                samples=samples,
                stop_event=stop,
                interval_s=args.sample_interval_seconds,
                t_start=t_start,
            )
        )

        # Fire requests concurrently. Each gets a unique task_id so
        # the checkpointer can't collapse them.
        request_tasks = [
            asyncio.create_task(
                _fire_one_request(
                    client=client,
                    task_id=f"repro-{uuid.uuid4().hex[:8]}",
                    request_timeout_s=args.request_timeout_seconds,
                )
            )
            for _ in range(args.concurrent_requests)
        ]

        # Wait for requests OR observation window, whichever is shorter.
        # In the hang case requests timeout; sampler keeps gathering
        # snapshots until the window closes.
        try:
            await asyncio.wait_for(
                asyncio.gather(*request_tasks, return_exceptions=True),
                timeout=args.observation_window_seconds,
            )
        except TimeoutError:
            logger.warning(
                "observation window (%ds) elapsed with requests still in flight — "
                "this is the hang signature",
                args.observation_window_seconds,
            )

        # Drain whatever finished, mark the rest as observation_timeout.
        for t in request_tasks:
            if t.done():
                try:
                    results.append(t.result())
                except Exception as exc:
                    results.append(
                        _RequestResult(
                            task_id="?",
                            elapsed_s=time.perf_counter() - t_start,
                            status="error",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
            else:
                # Take a final snapshot before cancelling so the artifact
                # shows the parking point at moment-of-give-up.
                try:
                    final = await asyncio.wait_for(client.get("/admin/asyncio-tasks"), timeout=5.0)
                    tasks_dump = final.json() if final.status_code == 200 else []
                    blocked = [t for t in tasks_dump if _is_checkpoint_blocked_at_aenter(t)]
                    pg = await _snapshot_pg_activity(args.postgres_url)
                    samples.append(
                        _Sample(
                            t_offset_s=time.perf_counter() - t_start,
                            asyncio_tasks_total=len(tasks_dump),
                            checkpoint_blocked_at_aenter=len(blocked),
                            pg_activity=pg,
                            blocked_coros=[t.get("coro", "") for t in blocked[:5]],
                        )
                    )
                except Exception:
                    pass
                t.cancel()
                results.append(
                    _RequestResult(
                        task_id="?",
                        elapsed_s=args.observation_window_seconds,
                        status="observation_timeout",
                        error="window closed before request returned",
                    )
                )

        stop.set()
        await sampler_task

    return _emit_summary_and_artifact(args, results, samples)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="repro_checkpoint_hang",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--postgres-url",
        default=os.environ.get(
            "POSTGRES_TEST_URL",
            "postgresql://repro:repro@localhost:55432/checkpoints",
        ),
        help=(
            "Postgres connection string. Default matches the bundled "
            "docker-compose.yml. Env var POSTGRES_TEST_URL also honored."
        ),
    )
    p.add_argument(
        "--concurrent-requests",
        type=int,
        default=5,
        help="Number of /inbox POSTs to fire concurrently. Default 5.",
    )
    p.add_argument(
        "--observation-window-seconds",
        type=int,
        default=120,
        help=(
            "Total wall-clock budget. Matches the 120s production timeout "
            "that surfaced the hang. Default 120."
        ),
    )
    p.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=60.0,
        help="Per-request timeout. Default 60.",
    )
    p.add_argument(
        "--sample-interval-seconds",
        type=float,
        default=2.0,
        help="Sampler tick interval. Default 2.0.",
    )
    p.add_argument(
        "--artifact",
        default="repro_checkpoint_hang.json",
        help="Where to write the JSON artifact. Default cwd.",
    )
    return p


def main() -> int:
    args = _build_argparser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
