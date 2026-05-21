-- Stage 2 — durable todo store.
--
-- Lives next to task_queue because the same DB connection / schema
-- privileges apply, but lifecycle is independent of the task queue:
-- todos persist until the operator marks them done or dropped, they
-- don't expire on TTL, they don't get claimed by workers, and they
-- don't carry idempotency keys.
--
-- Goal: replace Claude Code's session-local TaskCreate/TaskUpdate as
-- the durable surface for "things I want to do later." The CLI
-- (`hai todo add/ls/done`) is the primary interface; an MCP server
-- in front of this table is a v2 concern.

CREATE TABLE IF NOT EXISTS todo (
    id            TEXT PRIMARY KEY,                          -- ULID at insert time
    body          TEXT NOT NULL,                              -- the todo text (markdown-friendly)
    status        TEXT NOT NULL DEFAULT 'open',               -- open | done | dropped
    created_by    TEXT NOT NULL DEFAULT 'rob',                -- mirrors task_queue.requester semantics
    tags          TEXT[] NOT NULL DEFAULT '{}',               -- free-form labels
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,         -- whatever the client wants to stash
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at     TIMESTAMPTZ,                                -- set when status flips off 'open'
    CONSTRAINT todo_status_chk CHECK (status IN ('open', 'done', 'dropped'))
);

-- Hot index for the default "what's open?" query.
CREATE INDEX IF NOT EXISTS idx_todo_open
    ON todo (created_at)
    WHERE status = 'open';

-- Used by tag-filtered listings.
CREATE INDEX IF NOT EXISTS idx_todo_tags
    ON todo USING GIN (tags);
