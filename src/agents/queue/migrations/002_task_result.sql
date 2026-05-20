-- Phase 4.M2 — add `result` column for /admin/tasks/<id> retrieval.
--
-- The worker writes the graph's final `state.output` here on ack.
-- The /admin/tasks/<id> endpoint reads it for poll-based clients
-- (langgraph-daily-digest, future polling callers).

ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS result JSONB;
