-- Phase 4.M4 — guardian approval queue surface.
--
-- Applied idempotently at app startup by `agents.queue.migrate.ensure_schema`.
--
-- Adds a durable representation of the HOMELAB-SPEC Layer 4 Guardian
-- "awaiting approval" state. Before this, a task that paused on an
-- ApprovalRequest interrupt was acked `status='done'` by the worker —
-- the durable queue row lied about a task that was actually parked in
-- the checkpointer waiting on Rob. Now the worker parks the row at
-- `status='awaiting_approval'` and the resume path acks it `done`.
--
-- `approval_expires_at` carries the Layer 5 guardian TTL. On expiry the
-- task does NOT auto-execute: a guardian sweep moves the row to the DLQ
-- tagged `approval_ttl_expired` and notifies Rob.

-- Layer 5 guardian TTL deadline for awaiting-approval rows. NULL for
-- rows that never paused for approval.
ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS approval_expires_at TIMESTAMPTZ;

-- Guardian sweep + /admin/tasks status listing: find awaiting-approval
-- rows by their TTL deadline without scanning the checkpointer.
CREATE INDEX IF NOT EXISTS idx_task_queue_awaiting
    ON task_queue (approval_expires_at)
    WHERE status = 'awaiting_approval';

-- Status vocabulary is free TEXT (no enum constraint); document the
-- new value alongside the original three.
COMMENT ON COLUMN task_queue.status IS 'pending | claimed | awaiting_approval | done';
