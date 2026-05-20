-- Phase 4.M1 — task queue substrate (CNPG LISTEN/NOTIFY).
--
-- Applied idempotently at app startup by `agents.queue.migrate.ensure_schema`.
-- Lives in the same Postgres database as the LangGraph checkpointer
-- (`postgres-langgraph-checkpoints`); reuses its Barman ObjectStore for DR.

CREATE TABLE IF NOT EXISTS task_queue (
    id              TEXT PRIMARY KEY,                       -- ULID
    envelope        JSONB NOT NULL,                          -- full InboxRequest body
    status          TEXT NOT NULL DEFAULT 'pending',        -- pending | claimed | done
    attempts        INT NOT NULL DEFAULT 0,
    claimed_at      TIMESTAMPTZ,
    claimed_by      TEXT,                                    -- worker identity (pod name + uuid)
    visibility_timeout_at TIMESTAMPTZ,                       -- when claim expires
    ttl_expires_at  TIMESTAMPTZ,                             -- derived from envelope.ttl_seconds
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Pending claims: covers `dequeue` workload (find the oldest pending task).
CREATE INDEX IF NOT EXISTS idx_task_queue_pending
    ON task_queue (created_at)
    WHERE status = 'pending';

-- Visibility-timeout reclaim: find claimed tasks whose worker disappeared.
CREATE INDEX IF NOT EXISTS idx_task_queue_visibility
    ON task_queue (visibility_timeout_at)
    WHERE status = 'claimed';

-- Idempotency key dedup (defense-in-depth alongside the Dragonfly cache;
-- Postgres unique constraint guarantees no duplicate on race). NULL
-- idempotency_key is permitted (most tasks won't carry one).
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_queue_idempotency_key
    ON task_queue ((envelope ->> 'idempotency_key'))
    WHERE envelope ? 'idempotency_key';

CREATE TABLE IF NOT EXISTS task_dlq (
    id              TEXT PRIMARY KEY,                       -- ULID (same as original task)
    envelope        JSONB NOT NULL,
    attempts        INT NOT NULL,
    last_error      TEXT,
    dlq_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_dlq_dlq_at
    ON task_dlq (dlq_at DESC);

-- NOTIFY channel used by `TaskQueue.enqueue` to wake workers listening
-- via `LISTEN task_queue_new`. Workers fall back to polling on
-- visibility-timeout cadence regardless.
COMMENT ON TABLE task_queue IS 'Phase 4.M1 — Layer 5 task queue substrate';
COMMENT ON TABLE task_dlq IS 'Phase 4.M1 — DLQ for tasks past retry policy';
