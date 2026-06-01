"""Tests for the worker park-vs-ack branch (guardian approval queue).

HOMELAB-SPEC Layer 4 Guardian: a task that paused on an ApprovalRequest
interrupt is NOT done — it is parked at ``status='awaiting_approval'``
waiting on Rob's verdict. Before this branch the worker acked every
graph return ``done``, so the durable queue lied about a task still
alive in the checkpointer.

These tests pin: a paused task parks (and does not ack); a normally
completed task still acks (regression guard on the live dispatch path);
and the approval deadline honors the envelope TTL, falling back to the
configured default.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.queue.store import TaskClaim
from agents.queue.worker import QueueWorker
from agents.settings import get_settings

_CURATED = {
    "payload_summary": "Increase Frigate memory 4Gi→6Gi",
    "action_class": "C",
    "proposed_by": "smart-home-operator",
    "undo_path": "revert to 4Gi",
    "cost_estimate_usd": 0.0,
    "requires_two_person": False,
}


class _FakeQueue:
    def __init__(self) -> None:
        self.parked: list[tuple[str, datetime, dict[str, Any] | None]] = []

    async def park_for_approval(
        self,
        task_id: str,
        deadline: datetime,
        approval_request: dict[str, Any] | None = None,
    ) -> None:
        self.parked.append((task_id, deadline, approval_request))

    async def attempts_remaining(self, _claim: TaskClaim) -> int:
        return 3


def _build_worker(queue: _FakeQueue) -> QueueWorker:
    return QueueWorker(queue=queue, pool=MagicMock(), graph=MagicMock())


def _claim(**envelope: Any) -> TaskClaim:
    base: dict[str, Any] = {"content": "do the thing", "source": "test"}
    base.update(envelope)
    return TaskClaim(task_id="01J-task", envelope=base, attempts=1)


def _patch_branch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    paused: bool,
    output: str | None,
) -> tuple[list[str], list[str]]:
    """Stub the surrounding I/O so `_handle_one` exercises only the branch.

    Returns (acked, posted) recorder lists.
    """
    acked: list[str] = []
    posted: list[str] = []

    async def _fake_get_pending(_graph: Any, _task_id: str) -> dict[str, Any] | None:
        return dict(_CURATED) if paused else None

    async def _fake_invoke(_self: Any, _task_id: str, _envelope: dict[str, Any]) -> str | None:
        return output

    async def _fake_ack(_self: Any, task_id: str, _output: str | None) -> None:
        acked.append(task_id)

    async def _fake_post_approval(_graph: Any, task_id: str, **_kw: Any) -> None:
        posted.append(task_id)

    async def _fake_completion(_self: Any, *_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("agents.queue.worker.get_pending_approval", _fake_get_pending)
    monkeypatch.setattr("agents.queue.worker.post_approval_for_interrupts", _fake_post_approval)
    monkeypatch.setattr(QueueWorker, "_invoke_graph", _fake_invoke)
    monkeypatch.setattr(QueueWorker, "_ack_with_result", _fake_ack)
    monkeypatch.setattr(QueueWorker, "_post_completion", _fake_completion)
    return acked, posted


def test_handle_one_parks_when_approval_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paused on an approval interrupt → park at awaiting_approval, fire the
    webhook, and do NOT ack done."""
    acked, posted = _patch_branch(monkeypatch, paused=True, output=None)
    queue = _FakeQueue()
    worker = _build_worker(queue)

    asyncio.run(worker._handle_one(_claim()))

    assert len(queue.parked) == 1
    task_id, deadline, approval_request = queue.parked[0]
    assert task_id == "01J-task"
    assert isinstance(deadline, datetime)
    # Curated subset is persisted on the parked row for the guardian listing.
    assert approval_request == _CURATED
    assert posted == ["01J-task"]  # approval webhook fired
    assert acked == []  # NOT acked done


def test_handle_one_acks_when_no_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: a normally completed task still acks done and never
    parks — the live dispatch path must be unchanged for non-approval tasks."""
    acked, posted = _patch_branch(monkeypatch, paused=False, output="all done")
    queue = _FakeQueue()
    worker = _build_worker(queue)

    asyncio.run(worker._handle_one(_claim()))

    assert acked == ["01J-task"]
    assert queue.parked == []
    assert posted == []  # no approval webhook on the happy path


def test_approval_deadline_uses_envelope_ttl() -> None:
    """An explicit envelope ttl_seconds drives the deadline."""
    before = datetime.now(UTC)
    deadline = QueueWorker._approval_deadline({"ttl_seconds": 10})
    delta = (deadline - before).total_seconds()
    assert 9 <= delta <= 12


def test_approval_deadline_falls_back_to_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """No envelope ttl_seconds → the cluster-wide approval_ttl_seconds default."""
    monkeypatch.setenv("APPROVAL_TTL_SECONDS", "100")
    get_settings.cache_clear()
    before = datetime.now(UTC)
    deadline = QueueWorker._approval_deadline({})
    delta = (deadline - before).total_seconds()
    assert 95 <= delta <= 105
    get_settings.cache_clear()
