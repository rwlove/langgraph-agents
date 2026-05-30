"""Tests for the worker-side guardian approval-TTL sweep.

HOMELAB-SPEC Layer 4 Guardian + Layer 5: a task parked at
`awaiting_approval` whose `approval_expires_at` deadline lapses before
Rob answers does NOT auto-execute — it moves to the DLQ tagged
`approval_ttl_expired` and notifies Rob. The worker runs
`TaskQueue.expire_overdue_approvals()` on its own throttled cadence,
independent of the pre-run TTL sweep.

These tests pin: the approval sweep is throttled to one call per
interval on its own clock, it notifies only when something lapsed, the
notification distinguishes a lapsed approval from a pre-run timeout, and
a Pushover/DB failure never stalls the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.observability import (
    langgraph_awaiting_approval_oldest_age_seconds,
    langgraph_awaiting_approval_tasks,
)
from agents.queue.store import TaskClaim
from agents.queue.worker import QueueWorker


class _FakeQueue:
    def __init__(
        self,
        expired: list[TaskClaim] | None = None,
        *,
        stats: tuple[int, float] = (0, 0.0),
    ) -> None:
        self._expired = expired or []
        self._stats = stats
        self.approval_expire_calls = 0
        self.ttl_expire_calls = 0
        self.stats_calls = 0
        self.raise_on_expire = False
        self.raise_on_stats = False

    async def expire_overdue_approvals(self) -> list[TaskClaim]:
        self.approval_expire_calls += 1
        if self.raise_on_expire:
            raise RuntimeError("db hiccup")
        return self._expired

    async def expire_overdue(self) -> list[TaskClaim]:
        self.ttl_expire_calls += 1
        return []

    async def awaiting_approval_stats(self) -> tuple[int, float]:
        self.stats_calls += 1
        if self.raise_on_stats:
            raise RuntimeError("stats hiccup")
        return self._stats


def _build_worker(queue: _FakeQueue, *, sweep_interval: float = 60.0) -> QueueWorker:
    return QueueWorker(
        queue=queue,  # type: ignore[arg-type]
        pool=MagicMock(),
        graph=MagicMock(),
        sweep_interval_seconds=sweep_interval,
    )


def test_approval_sweep_runs_on_first_call_then_throttles() -> None:
    """First call sweeps (last_approval_sweep=0); an immediate second is throttled."""
    queue = _FakeQueue()
    worker = _build_worker(queue, sweep_interval=60.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())
    asyncio.run(worker._maybe_sweep_expired_approvals())

    assert queue.approval_expire_calls == 1


def test_approval_sweep_runs_again_after_interval() -> None:
    """With a zero interval, every call sweeps."""
    queue = _FakeQueue()
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())
    asyncio.run(worker._maybe_sweep_expired_approvals())

    assert queue.approval_expire_calls == 2


def test_approval_sweep_has_independent_clock_from_ttl_sweep() -> None:
    """The approval sweep must not share a throttle clock with the TTL sweep —
    otherwise running one would starve the other within an interval."""
    queue = _FakeQueue()
    worker = _build_worker(queue, sweep_interval=60.0)

    asyncio.run(worker._maybe_sweep_expired())
    # The TTL sweep just consumed its clock; the approval sweep should still
    # fire on its own first call.
    asyncio.run(worker._maybe_sweep_expired_approvals())

    assert queue.ttl_expire_calls == 1
    assert queue.approval_expire_calls == 1


def test_approval_sweep_notifies_when_approvals_lapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lapsed rows → a Pushover summary naming the reaped task IDs and making
    clear the verdict never came (vs. a pre-run timeout)."""
    sent: list[tuple[str, str]] = []

    def _fake_send(title: str, message: str, **_kw: Any) -> None:
        sent.append((title, message))

    monkeypatch.setattr("agents.queue.worker.pushover.send", _fake_send)

    expired = [
        TaskClaim(task_id="01J-aaa", envelope={"intent": "delete namespace foo"}, attempts=0),
        TaskClaim(task_id="01J-bbb", envelope={"content": "rotate db secret"}, attempts=1),
    ]
    queue = _FakeQueue(expired)
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())

    assert len(sent) == 1
    title, message = sent[0]
    assert "Approvals expired" in title
    assert "2 task(s)" in message
    assert "awaiting your approval" in message
    assert "01J-aaa" in message
    assert "delete namespace foo" in message
    assert "01J-bbb" in message
    assert "rotate db secret" in message


def test_approval_sweep_silent_when_nothing_lapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No overdue approvals → no Pushover send."""
    sent: list[Any] = []
    monkeypatch.setattr(
        "agents.queue.worker.pushover.send",
        lambda *a, **k: sent.append((a, k)),
    )
    queue = _FakeQueue([])
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())

    assert sent == []


def test_approval_sweep_swallows_db_error() -> None:
    """A failing expire_overdue_approvals must not propagate out of the helper."""
    queue = _FakeQueue()
    queue.raise_on_expire = True
    worker = _build_worker(queue, sweep_interval=0.0)

    # No exception propagates.
    asyncio.run(worker._maybe_sweep_expired_approvals())


def test_approval_notify_swallows_pushover_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pushover failure is best-effort — rows are already in the DLQ."""

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("pushover unconfigured")

    monkeypatch.setattr("agents.queue.worker.pushover.send", _boom)

    expired = [TaskClaim(task_id="01J-ccc", envelope={"intent": "x"}, attempts=0)]
    queue = _FakeQueue(expired)
    worker = _build_worker(queue, sweep_interval=0.0)

    # No exception propagates.
    asyncio.run(worker._maybe_sweep_expired_approvals())


def test_approval_notify_truncates_long_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large lapse batch caps the per-task lines and notes the remainder."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agents.queue.worker.pushover.send",
        lambda title, message, **_k: sent.append((title, message)),
    )

    expired = [
        TaskClaim(task_id=f"01J-{i:03d}", envelope={"intent": f"task {i}"}, attempts=0)
        for i in range(15)
    ]
    queue = _FakeQueue(expired)
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())

    _title, message = sent[0]
    assert "15 task(s)" in message
    assert "… and 5 more" in message


def test_approval_sweep_refreshes_depth_and_age_gauges() -> None:
    """Each sweep publishes the guardian-queue depth + oldest-age to Prometheus."""
    queue = _FakeQueue(stats=(3, 4200.0))
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())

    assert queue.stats_calls == 1
    assert langgraph_awaiting_approval_tasks._value.get() == 3
    assert langgraph_awaiting_approval_oldest_age_seconds._value.get() == 4200.0


def test_approval_sweep_swallows_stats_error() -> None:
    """A failing stats query must not propagate or stall the loop."""
    queue = _FakeQueue()
    queue.raise_on_stats = True
    worker = _build_worker(queue, sweep_interval=0.0)

    # No exception propagates; the expiry sweep still ran.
    asyncio.run(worker._maybe_sweep_expired_approvals())
    assert queue.approval_expire_calls == 1


def test_approval_sweep_skips_stats_when_expiry_errors() -> None:
    """If the expiry sweep itself errors, we bail before the stats query."""
    queue = _FakeQueue()
    queue.raise_on_expire = True
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired_approvals())
    assert queue.stats_calls == 0
