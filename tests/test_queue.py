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
from agents.queue.migrate import _split_statements
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


def test_migrate_split_statements_handles_multi_statement_file() -> None:
    """Real migration files contain CREATE TABLE + multiple CREATE INDEX —
    they must split into individual statements so psycopg's extended
    query protocol (forced into "prepare every statement" mode by the
    checkpointer pool's prepare_threshold=0) doesn't choke on the
    multi-statement form.
    """
    sql = resources.files("agents.queue.migrations").joinpath("001_task_queue.sql").read_text()
    stmts = _split_statements(sql)
    # Real shape: 2 CREATE TABLE + 3 CREATE INDEX + 1 CREATE UNIQUE INDEX
    # + 2 COMMENT ON TABLE = 8 statements (give or take if the file is
    # edited; pin the lower bound).
    assert len(stmts) >= 5
    # No `;` should remain inside any statement (we stripped them).
    assert all(";" not in s for s in stmts)
    # Comment-only lines must be filtered.
    assert all(not s.startswith("--") for s in stmts)
    # First statement should be the CREATE TABLE task_queue.
    assert "CREATE TABLE IF NOT EXISTS task_queue" in stmts[0]


def test_enqueue_uses_pg_notify_function_not_command() -> None:
    """`NOTIFY <channel>, $1` is rejected by the Postgres parser — bind
    parameters can only flow into ``pg_notify(text, text)``. Pin the SQL
    shape so a future edit doesn't regress to the broken form.
    """
    src = resources.files("agents.queue").joinpath("store.py").read_text()
    assert "pg_notify" in src, "enqueue should call pg_notify() — not the NOTIFY command"
    # The broken form would look like `NOTIFY {NOTIFY_CHANNEL}, %s`.
    assert "NOTIFY {NOTIFY_CHANNEL}" not in src
    assert "NOTIFY {channel}" not in src


def test_migrate_split_statements_strips_comment_lines() -> None:
    """A `;` inside a -- comment line must not split the statement."""
    sql = """
    -- this has a ; embedded in a comment
    CREATE TABLE foo (id INT);
    -- another comment
    CREATE INDEX foo_idx ON foo(id);
    """
    stmts = _split_statements(sql)
    assert len(stmts) == 2
    assert "CREATE TABLE foo" in stmts[0]
    assert "CREATE INDEX foo_idx" in stmts[1]


def test_migrate_split_statements_handles_trailing_no_semicolon() -> None:
    """The last statement may omit the trailing semicolon."""
    sql = "CREATE TABLE foo (id INT);\nALTER TABLE foo ADD COLUMN x INT"
    stmts = _split_statements(sql)
    assert stmts == ["CREATE TABLE foo (id INT)", "ALTER TABLE foo ADD COLUMN x INT"]


def test_dequeue_excludes_ttl_expired_tasks() -> None:
    """`dequeue` must never claim a task past its TTL.

    HOMELAB-SPEC Layer 5: an expired task does NOT auto-execute. The
    guard lives in the inner SELECT so a dead row is skipped atomically
    under SKIP LOCKED. Pin the SQL shape so a future edit can't drop it.
    """
    src = resources.files("agents.queue").joinpath("store.py").read_text()
    assert "ttl_expires_at IS NULL OR ttl_expires_at > NOW()" in src


def test_expire_overdue_routes_to_dlq_atomically() -> None:
    """`expire_overdue` moves overdue rows to the DLQ in one CTE.

    The DELETE + DLQ INSERT must commit together (a task is never lost
    between tables) and the marker must be `ttl_expired` so the 4.M3
    DLQ surface distinguishes timed-out tasks from retry-exhausted ones.
    """
    src = resources.files("agents.queue").joinpath("store.py").read_text()
    assert "async def expire_overdue" in src
    assert "DELETE FROM task_queue" in src
    assert "ttl_expires_at < NOW()" in src
    assert "'ttl_expired'" in src
    # Both never-claimed and stuck-claim rows are caught.
    assert "status IN ('pending', 'claimed')" in src


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
