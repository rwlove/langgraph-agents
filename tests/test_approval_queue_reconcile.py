"""Tests for /approval reconciling the durable queue row after a resume.

The worker parks a task at ``status='awaiting_approval'`` when it first
hits an ApprovalRequest interrupt (queue/worker.py). When Rob's verdict
arrives at /approval and the graph resumes, the queue row must be
reconciled with what the graph actually did:

- approve / reject run the action to a terminal state → ack ``done`` via
  ``resolve_approval``.
- defer re-interrupts (graphs/approval.py) → the graph is paused again,
  so re-park with a fresh deadline rather than acking done.

The handler branches on the POST-resume graph state, not on the reaction,
so it stays correct for any future multi-step approval. Smoke tasks and
queue-disabled deployments skip reconciliation entirely.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api.approval import router


class _FakeQueue:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str | None]] = []
        self.parked: list[tuple[str, datetime]] = []

    async def resolve_approval(self, task_id: str, output: str | None) -> None:
        self.resolved.append((task_id, output))

    async def park_for_approval(
        self, task_id: str, deadline: datetime, approval_request: dict | None = None
    ) -> None:
        self.parked.append((task_id, deadline))


def _paused_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.tasks = [MagicMock()]
    snap.tasks[0].interrupts = [MagicMock()]
    return snap


def _terminal_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.tasks = []
    return snap


def _build_app(graph: Any, queue: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.graph = graph
    app.state.smoke_graph = None
    app.state.task_queue = queue
    return app


def test_approve_acks_done_via_resolve_approval() -> None:
    """A terminal post-resume state acks the queue row done."""
    graph = MagicMock()
    # Pre-resume check sees paused; post-resume check sees terminal.
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _terminal_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": "did the thing"})
    queue = _FakeQueue()
    client = TestClient(_build_app(graph, queue))

    r = client.post(
        "/approval",
        json={
            "task_id": "01J-prod-task",
            "reaction": "approve",
            "approval_token": "tok",
        },
    )

    assert r.status_code == 200
    assert r.json()["status"] == "complete"
    assert queue.resolved == [("01J-prod-task", "did the thing")]
    assert queue.parked == []


def test_defer_reparks_with_fresh_deadline() -> None:
    """A re-interrupted (still-paused) post-resume state re-parks, not acks."""
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _paused_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": None})
    queue = _FakeQueue()
    client = TestClient(_build_app(graph, queue))

    r = client.post(
        "/approval",
        json={
            "task_id": "01J-prod-task",
            "reaction": "defer",
            "approval_token": "tok",
        },
    )

    assert r.status_code == 200
    assert r.json()["status"] == "resumed"
    assert queue.resolved == []
    assert len(queue.parked) == 1
    task_id, deadline = queue.parked[0]
    assert task_id == "01J-prod-task"
    assert isinstance(deadline, datetime)


def test_smoke_task_skips_queue_reconcile() -> None:
    """Smoke task_ids never entered the durable queue — no resolve/park."""
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_paused_snapshot())
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    queue = _FakeQueue()
    app = FastAPI()
    app.include_router(router)
    app.state.graph = None
    app.state.smoke_graph = graph
    app.state.task_queue = queue
    client = TestClient(app)

    r = client.post(
        "/approval",
        json={
            "task_id": "smoke-verify-20260530",
            "reaction": "approve",
            "approval_token": "tok",
        },
    )

    assert r.status_code == 200
    # Only one aget_state (the pre-resume 409 guard); no second reconcile read.
    assert graph.aget_state.call_count == 1
    assert queue.resolved == []
    assert queue.parked == []


def test_queue_disabled_deployment_skips_reconcile() -> None:
    """task_queue is None when the queue is disabled — handler must not crash."""
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_paused_snapshot())
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    app = FastAPI()
    app.include_router(router)
    app.state.graph = graph
    app.state.smoke_graph = None
    app.state.task_queue = None
    client = TestClient(app)

    r = client.post(
        "/approval",
        json={
            "task_id": "01J-prod-task",
            "reaction": "approve",
            "approval_token": "tok",
        },
    )

    assert r.status_code == 200
    assert graph.aget_state.call_count == 1
