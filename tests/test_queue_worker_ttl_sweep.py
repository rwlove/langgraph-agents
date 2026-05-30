"""Tests for the worker-side TTL expiry sweep.

HOMELAB-SPEC Layer 5: a task whose TTL elapses before it runs does NOT
auto-execute — it expires and notifies Rob with a summary. The worker
runs `TaskQueue.expire_overdue()` on a throttled cadence and fires a
Pushover summary when rows are reaped.

These tests pin: the sweep is throttled to one call per interval, it
notifies only when something expired, the notification summarizes the
reaped tasks, and a Pushover/DB failure never stalls the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.queue.store import TaskClaim
from agents.queue.worker import QueueWorker


class _FakeQueue:
    def __init__(self, expired: list[TaskClaim] | None = None) -> None:
        self._expired = expired or []
        self.expire_calls = 0
        self.raise_on_expire = False

    async def expire_overdue(self) -> list[TaskClaim]:
        self.expire_calls += 1
        if self.raise_on_expire:
            raise RuntimeError("db hiccup")
        return self._expired


def _build_worker(queue: _FakeQueue, *, sweep_interval: float = 60.0) -> QueueWorker:
    return QueueWorker(
        queue=queue,  # type: ignore[arg-type]
        pool=MagicMock(),
        graph=MagicMock(),
        sweep_interval_seconds=sweep_interval,
    )


def test_sweep_runs_on_first_call_then_throttles() -> None:
    """First call sweeps (last_sweep=0); an immediate second call is throttled."""
    queue = _FakeQueue()
    worker = _build_worker(queue, sweep_interval=60.0)

    asyncio.run(worker._maybe_sweep_expired())
    asyncio.run(worker._maybe_sweep_expired())

    assert queue.expire_calls == 1


def test_sweep_runs_again_after_interval() -> None:
    """With a zero interval, every call sweeps."""
    queue = _FakeQueue()
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired())
    asyncio.run(worker._maybe_sweep_expired())

    assert queue.expire_calls == 2


def test_sweep_notifies_when_tasks_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired rows → a Pushover summary naming the reaped task IDs."""
    sent: list[tuple[str, str]] = []

    def _fake_send(title: str, message: str, **_kw: Any) -> None:
        sent.append((title, message))

    monkeypatch.setattr("agents.queue.worker.pushover.send", _fake_send)

    expired = [
        TaskClaim(task_id="01J-aaa", envelope={"intent": "restart frigate"}, attempts=0),
        TaskClaim(task_id="01J-bbb", envelope={"content": "rebalance gpus"}, attempts=1),
    ]
    queue = _FakeQueue(expired)
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired())

    assert len(sent) == 1
    _title, message = sent[0]
    assert "2 task(s)" in message
    assert "01J-aaa" in message
    assert "restart frigate" in message
    assert "01J-bbb" in message
    assert "rebalance gpus" in message


def test_sweep_silent_when_nothing_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """No overdue rows → no Pushover send."""
    sent: list[Any] = []
    monkeypatch.setattr(
        "agents.queue.worker.pushover.send",
        lambda *a, **k: sent.append((a, k)),
    )
    queue = _FakeQueue([])
    worker = _build_worker(queue, sweep_interval=0.0)

    asyncio.run(worker._maybe_sweep_expired())

    assert sent == []


def test_sweep_swallows_db_error() -> None:
    """A failing expire_overdue must not propagate out of the loop helper."""
    queue = _FakeQueue()
    queue.raise_on_expire = True
    worker = _build_worker(queue, sweep_interval=0.0)

    # No exception propagates.
    asyncio.run(worker._maybe_sweep_expired())


def test_notify_swallows_pushover_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Pushover failure is best-effort — rows are already in the DLQ."""

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("pushover unconfigured")

    monkeypatch.setattr("agents.queue.worker.pushover.send", _boom)

    expired = [TaskClaim(task_id="01J-ccc", envelope={"intent": "x"}, attempts=0)]
    queue = _FakeQueue(expired)
    worker = _build_worker(queue, sweep_interval=0.0)

    # No exception propagates.
    asyncio.run(worker._maybe_sweep_expired())


def test_notify_truncates_long_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large expiry batch caps the per-task lines and notes the remainder."""
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

    asyncio.run(worker._maybe_sweep_expired())

    _title, message = sent[0]
    assert "15 task(s)" in message
    assert "… and 5 more" in message
