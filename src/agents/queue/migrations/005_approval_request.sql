-- Phase 4.M4 — surface the approval decision on the guardian listing.
--
-- Applied idempotently at app startup by `agents.queue.migrate.ensure_schema`.
--
-- The `/admin/tasks?status=awaiting_approval` listing is the guardian
-- read surface (HA Companion approval card + phone notification). Until
-- now it returned only the originating prompt `content` — the agent's
-- instruction, not what Rob is actually approving. The human-meaningful
-- decision data (proposed action, undo path, who proposed it) lived only
-- in the per-task checkpointer interrupt, unreachable from the cheap
-- status-indexed query without a full checkpointer scan.
--
-- This column lets the worker persist a curated ApprovalRequest subset
-- at park time so the listing can render the decision without touching
-- the checkpointer. NULL for rows that never paused for approval.

ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS approval_request JSONB;
