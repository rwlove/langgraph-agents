"""Tests for the task-queue primitives (Phase 4.M1).

Real Postgres queue ops live behind a pool we can't construct in unit
tests without a database. These tests pin the schema-text, the ULID
generation, the `TaskClaim` dataclass shape, and the
`attempts_remaining` calculation.

Full integration against a live CNPG cluster comes in 4.M2's PR.
"""

from __future__ import annotations

import asyncio
from importlib import resources

from ulid import ULID

from agents.queue import TaskClaim, TaskQueue
from agents.queue.store import NOTIFY_CHANNEL


def test_notify_channel_constant() -> None:
    """The NOTIFY channel name is part of the contract with worker LISTEN."""
    assert NOTIFY_CHANNEL == "task_queue_new"


def test_taskclaim_dataclass() -> None:
    claim = TaskClaim(task_id="t-001", envelope={"foo": "bar"}, attempts=1)
    assert claim.task_id == "t-001"
    assert claim.envelope == {"foo": "bar"}
    assert claim.attempts == 1


def test_ulid_sort_order() -> None:
    """ULIDs sort lexicographically by creation time.

    Property the queue's `ORDER BY created_at` relies on: the row
    inserted earlier has a smaller ULID, so dequeue's "oldest first"
    semantic is correct even without consulting `created_at` (we keep
    the timestamp column too for clarity + index efficiency).
    """
    first = str(ULID())
    second = str(ULID())
    assert first < second


def test_migration_file_present() -> None:
    """Migration text is shipped in-package so prod containers find it."""
    migrations = resources.files("agents.queue.migrations")
    files = [f.name for f in migrations.iterdir() if f.name.endswith(".sql")]
    assert "001_task_queue.sql" in files


def test_migration_sql_idempotent_marks() -> None:
    """Every CREATE in the migration must be IF NOT EXISTS so re-running is safe."""
    sql = resources.files("agents.queue.migrations").joinpath("001_task_queue.sql").read_text()
    # Match the create lines case-insensitively.
    for line in sql.splitlines():
        upper = line.upper()
        if (
            upper.startswith("CREATE TABLE")
            or upper.startswith("CREATE INDEX")
            or upper.startswith("CREATE UNIQUE INDEX")
        ):
            assert "IF NOT EXISTS" in upper, f"non-idempotent CREATE: {line!r}"


def test_taskqueue_attempts_remaining_uses_envelope_policy() -> None:
    """`attempts_remaining` reads max from envelope.retry_policy when set."""

    class _FakePool:
        pass

    queue = TaskQueue(_FakePool(), default_max_attempts=3)

    # Envelope without retry_policy → default 3 - 0 attempts = 3 remaining
    claim = TaskClaim(task_id="t", envelope={}, attempts=0)
    assert asyncio.run(queue.attempts_remaining(claim)) == 3

    # Envelope with retry_policy.max_attempts=5 → 5 - 2 = 3 remaining
    claim_with_policy = TaskClaim(
        task_id="t",
        envelope={"retry_policy": {"max_attempts": 5}},
        attempts=2,
    )
    assert asyncio.run(queue.attempts_remaining(claim_with_policy)) == 3


def test_taskqueue_attempts_remaining_clamps_negative() -> None:
    """If attempts already > max, remaining = 0 (not negative)."""

    class _FakePool:
        pass

    queue = TaskQueue(_FakePool(), default_max_attempts=2)
    claim = TaskClaim(task_id="t", envelope={}, attempts=5)
    assert asyncio.run(queue.attempts_remaining(claim)) == 0
