-- Per-completion model-group provenance.
--
-- Applied idempotently at app startup by `agents.queue.migrate.ensure_schema`.
--
-- Records which model GROUP(s) actually served a task's LLM calls, so
-- `hai cost`'s group breakdown reports real escalation counts instead of
-- deriving them from the static `AGENT_GROUP` map. Before this, a runtime
-- escalation (`escalate=True` / `group_override="claude"` / the router
-- scorer) or a Spark-down degrade to P40 was invisible on the row — the
-- breakdown reported each task under its agent's *configured* group only.
--
-- `served_groups` is the sorted, distinct set of `effective_group` values
-- the worker observed across the task's run (e.g. `["claude","local-p40"]`
-- for a task whose triager ran local but whose executor escalated). NULL
-- on rows completed before this migration, and on rows the worker acked
-- without any recorded provenance (the accumulator is process-local — a
-- pod restart mid-task loses it, same constraint as the cost-cap
-- accumulators). The read path falls back to `AGENT_GROUP` for NULL rows.

ALTER TABLE task_queue ADD COLUMN IF NOT EXISTS served_groups JSONB;

COMMENT ON COLUMN task_queue.served_groups IS
    'Distinct effective model groups that served this task (JSONB array); NULL = derive from AGENT_GROUP';
