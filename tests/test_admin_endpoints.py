"""Admin endpoints used by the n8n awaiting-user-sweep + cost-cap-watcher."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from agents.api import admin
from agents.observability import langgraph_paused_threads
from agents.state import ALL_AGENT_IDS


def _read_paused_gauge(is_stale: str) -> float:
    """Read the current value of the paused-threads gauge for one label."""
    for metric in langgraph_paused_threads.collect():
        for sample in metric.samples:
            if sample.labels.get("is_stale") == is_stale:
                return sample.value
    return 0.0


def _make_app_with_graph(temp_vault: Path) -> FastAPI:
    """Build a FastAPI app with the admin router + a mock graph attribute.

    Sets both `graph` and `admin_graph` to None — tests assign per-case.
    Endpoints split between the two: read paths (`GET /admin/tasks`,
    `GET /admin/tasks/<id>`) read from `admin_graph`; write paths
    (`POST .../timeout-tier`, `.../cancel`) and the `paused-threads`
    sweep stay on `graph`.
    """
    app = FastAPI()
    app.include_router(admin.router)
    app.state.graph = None  # set per-test via fixture
    app.state.admin_graph = None
    app.state.queue_pool = None
    return app


def test_list_agents_returns_all_fleet_ids(temp_vault: Path) -> None:
    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/agents")
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert set(ids) == set(ALL_AGENT_IDS)


def test_timeout_tier_rejects_invalid_tier(temp_vault: Path) -> None:
    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.post("/admin/tasks/t-1/timeout-tier", json={"tier": "bogus"})
        assert r.status_code == 422  # Pydantic Literal rejection


def test_timeout_tier_404_when_task_unknown(temp_vault: Path) -> None:
    """get_state on an unknown thread returns an empty StateSnapshot;
    the endpoint should 404."""
    fake_graph = MagicMock()
    snap = MagicMock()
    snap.values = None  # signal: thread not found
    fake_graph.aget_state = AsyncMock(return_value=snap)

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.post("/admin/tasks/unknown-task/timeout-tier", json={"tier": "30min"})
        assert r.status_code == 404


def test_cancel_404_when_task_unknown(temp_vault: Path) -> None:
    fake_graph = MagicMock()
    snap = MagicMock()
    snap.values = None
    fake_graph.aget_state = AsyncMock(return_value=snap)

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.post("/admin/tasks/unknown-task/cancel", json={"reason": "test"})
        assert r.status_code == 404


def test_costs_today_returns_zero_when_no_log(temp_vault: Path) -> None:
    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/costs/today")
        assert r.status_code == 200
        body = r.json()
        assert body["spent_usd"] == 0.0
        assert body["daily_cap_usd"] == 30.0  # default cap
        assert body["per_agent"] == {}
        assert "date" in body


def test_costs_today_aggregates_from_log(temp_vault: Path) -> None:
    """Plant a cost log and verify the endpoint sums it."""
    today = datetime.now(UTC).date().isoformat()
    cost_dir = temp_vault / "reports" / "costs"
    cost_dir.mkdir(parents=True)
    (cost_dir / f"{today}.jsonl").write_text(
        '{"agent": "coder", "cost_usd": 1.5}\n'
        '{"agent": "coder", "cost_usd": 0.5}\n'
        '{"agent": "homelab-engineer", "cost_usd": 2.0}\n'
    )

    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/costs/today")
        assert r.status_code == 200
        body = r.json()
        assert body["spent_usd"] == 4.0
        assert body["per_agent"] == {"coder": 2.0, "homelab-engineer": 2.0}


def test_paused_threads_503_when_graph_missing(temp_vault: Path) -> None:
    """No graph on app.state → 503, not a confusing 500."""
    app = _make_app_with_graph(temp_vault)
    # graph stays None (set by _make_app_with_graph)
    with TestClient(app) as client:
        r = client.get("/admin/paused-threads")
        assert r.status_code == 503


def _empty_alist() -> AsyncIterator[Any]:
    async def _gen() -> AsyncIterator[Any]:
        if False:  # never yields
            yield None

    return _gen()


def test_paused_threads_returns_empty_list_when_no_pauses(
    temp_vault: Path,
) -> None:
    """No checkpoints → 200 + []. The happy path for a fresh cluster."""
    fake_graph = MagicMock()
    fake_checkpointer = MagicMock()
    fake_checkpointer.alist = MagicMock(side_effect=lambda *a, **kw: _empty_alist())
    fake_graph.checkpointer = fake_checkpointer

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.get("/admin/paused-threads")
        assert r.status_code == 200
        assert r.json() == []


def test_paused_threads_skips_when_memory_saver(temp_vault: Path) -> None:
    """MemorySaver dev path returns [] (sweep is a no-op)."""
    fake_graph = MagicMock()
    fake_graph.checkpointer = MemorySaver()

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.get("/admin/paused-threads")
        assert r.status_code == 200
        assert r.json() == []


def test_paused_threads_reports_paused_with_staleness(temp_vault: Path) -> None:
    """A thread paused 1h ago + a thread paused 10s ago — staleness flags correctly."""
    # CheckpointTuple-shaped objects (we only need `.config`).
    cp_stale = MagicMock()
    cp_stale.config = {"configurable": {"thread_id": "stale-1"}}
    cp_fresh = MagicMock()
    cp_fresh.config = {"configurable": {"thread_id": "fresh-1"}}

    async def _alist(*_a: Any, **_kw: Any) -> AsyncIterator[Any]:
        for cp in (cp_stale, cp_fresh):
            yield cp

    # Each thread resolves to a snapshot with one interrupt + a different created_at.
    interrupt = MagicMock()
    interrupt.id = "ix-1"
    interrupt.value = {"reason": "test"}
    task = MagicMock()
    task.interrupts = [interrupt]

    now = datetime.now(UTC)
    stale_snap = MagicMock()
    stale_snap.tasks = [task]
    stale_snap.created_at = (now - timedelta(hours=1)).isoformat()
    stale_snap.next = ("supervisor",)

    fresh_snap = MagicMock()
    fresh_snap.tasks = [task]
    fresh_snap.created_at = (now - timedelta(seconds=10)).isoformat()
    fresh_snap.next = ("supervisor",)

    async def _aget_state(config: dict[str, Any]) -> Any:
        tid = config["configurable"]["thread_id"]
        return stale_snap if tid == "stale-1" else fresh_snap

    fake_graph = MagicMock()
    fake_graph.checkpointer = MagicMock()
    fake_graph.checkpointer.alist = MagicMock(side_effect=lambda *a, **kw: _alist())
    fake_graph.aget_state = AsyncMock(side_effect=_aget_state)

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.get("/admin/paused-threads")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2

        by_id = {row["thread_id"]: row for row in body}
        assert by_id["stale-1"]["is_stale"] is True
        assert by_id["stale-1"]["paused_for_seconds"] > 60
        assert by_id["stale-1"]["interrupts"][0]["id"] == "ix-1"
        assert by_id["stale-1"]["next"] == ["supervisor"]

        assert by_id["fresh-1"]["is_stale"] is False
        assert by_id["fresh-1"]["paused_for_seconds"] < 60

        # Gauge should reflect the sweep: 1 stale, 1 non-stale.
        assert _read_paused_gauge("true") == 1.0
        assert _read_paused_gauge("false") == 1.0


def test_paused_threads_gauge_resets_when_sweep_empty(temp_vault: Path) -> None:
    """After a populated sweep, an empty sweep must zero the gauge for both labels.

    Prevents a "phantom stale" — once a thread resumes (or the pod
    restarts after dropping the checkpoint), the next sweep must
    bring the gauge back to 0 so the alert clears.
    """
    # Pre-populate the gauge with non-zero values via a direct call.
    langgraph_paused_threads.labels(is_stale="true").set(7)
    langgraph_paused_threads.labels(is_stale="false").set(3)
    assert _read_paused_gauge("true") == 7.0

    fake_graph = MagicMock()
    fake_checkpointer = MagicMock()
    fake_checkpointer.alist = MagicMock(side_effect=lambda *a, **kw: _empty_alist())
    fake_graph.checkpointer = fake_checkpointer

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.get("/admin/paused-threads")
        assert r.status_code == 200
        assert r.json() == []

    # Both labels reset to 0.
    assert _read_paused_gauge("true") == 0.0
    assert _read_paused_gauge("false") == 0.0


def test_paused_threads_skips_threads_without_interrupts(
    temp_vault: Path,
) -> None:
    """Non-paused checkpoints in the same alist must not leak into the output."""
    cp = MagicMock()
    cp.config = {"configurable": {"thread_id": "running-1"}}

    async def _alist(*_a: Any, **_kw: Any) -> AsyncIterator[Any]:
        yield cp

    # A snapshot whose tasks have no interrupts == thread is running normally.
    task = MagicMock()
    task.interrupts = []
    snap = MagicMock()
    snap.tasks = [task]
    snap.created_at = datetime.now(UTC).isoformat()
    snap.next = ()

    fake_graph = MagicMock()
    fake_graph.checkpointer = MagicMock()
    fake_graph.checkpointer.alist = MagicMock(side_effect=lambda *a, **kw: _alist())
    fake_graph.aget_state = AsyncMock(return_value=snap)

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.get("/admin/paused-threads")
        assert r.status_code == 200
        assert r.json() == []


def test_list_tasks_uses_admin_graph(temp_vault: Path) -> None:
    """`GET /admin/tasks` MUST read from `app.state.admin_graph`, not `graph`.

    The whole point of the dedicated admin saver is that the read path
    can't be blocked by the main checkpointer's lock. If a refactor
    accidentally points `list_tasks` back at `state.graph`, the
    contention bug returns silently.
    """
    cp = MagicMock()
    cp.config = {"configurable": {"thread_id": "t-admin"}}

    async def _alist(*_a: Any, **_kw: Any) -> AsyncIterator[Any]:
        yield cp

    interrupt = MagicMock()
    interrupt.id = "ix-1"
    interrupt.value = {"reason": "test"}
    task = MagicMock()
    task.interrupts = [interrupt]
    snap = MagicMock()
    snap.tasks = [task]
    snap.values = {
        "target_agent": "coder",
        "awaiting_user_since": "2026-05-21T12:00:00+00:00",
        "timeout_tier": None,
    }

    admin_graph = MagicMock()
    admin_graph.checkpointer = MagicMock()
    admin_graph.checkpointer.alist = MagicMock(side_effect=lambda *a, **kw: _alist())
    admin_graph.aget_state = AsyncMock(return_value=snap)

    # `state.graph` left as a sentinel that, if accidentally used, raises.
    sentinel_main = MagicMock()
    sentinel_main.checkpointer = MagicMock()
    sentinel_main.checkpointer.alist = MagicMock(
        side_effect=AssertionError("list_tasks must not touch state.graph")
    )

    app = _make_app_with_graph(temp_vault)
    app.state.graph = sentinel_main
    app.state.admin_graph = admin_graph
    with TestClient(app) as client:
        r = client.get("/admin/tasks")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["task_id"] == "t-admin"
        assert body[0]["target_agent"] == "coder"
        assert body[0]["interrupts"][0]["id"] == "ix-1"


def test_list_tasks_503_when_admin_graph_missing(temp_vault: Path) -> None:
    """No admin graph on app.state → 503 with an explicit error."""
    app = _make_app_with_graph(temp_vault)
    # Both graphs stay None (set by _make_app_with_graph).
    with TestClient(app) as client:
        r = client.get("/admin/tasks")
        assert r.status_code == 503
        assert "admin graph" in r.json()["detail"]


def test_get_task_checkpointer_uses_admin_graph(temp_vault: Path) -> None:
    """`GET /admin/tasks/<id>` checkpointer read uses `admin_graph`."""
    interrupt = MagicMock()
    interrupt.id = "ix-9"
    interrupt.value = {"reason": "test"}
    task = MagicMock()
    task.interrupts = [interrupt]
    snap = MagicMock()
    snap.tasks = [task]
    snap.values = {"target_agent": "coder"}
    snap.next = ("supervisor",)

    admin_graph = MagicMock()
    admin_graph.aget_state = AsyncMock(return_value=snap)

    sentinel_main = MagicMock()
    sentinel_main.aget_state = AsyncMock(
        side_effect=AssertionError("get_task must not touch state.graph"),
    )

    app = _make_app_with_graph(temp_vault)
    app.state.graph = sentinel_main
    app.state.admin_graph = admin_graph
    with TestClient(app) as client:
        r = client.get("/admin/tasks/t-9")
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] == "t-9"
        assert body["checkpointer"]["values"] == {"target_agent": "coder"}
        assert body["checkpointer"]["interrupts"][0]["id"] == "ix-9"


def test_costs_today_tolerates_malformed_log(temp_vault: Path) -> None:
    """Bad JSON line shouldn't crash the endpoint."""
    today = datetime.now(UTC).date().isoformat()
    cost_dir = temp_vault / "reports" / "costs"
    cost_dir.mkdir(parents=True)
    (cost_dir / f"{today}.jsonl").write_text(
        '{"agent": "coder", "cost_usd": 1.0}\nthis is not json\n'
    )

    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/costs/today")
        # The endpoint catches the ValueError and returns zeros (graceful degrade)
        assert r.status_code == 200
