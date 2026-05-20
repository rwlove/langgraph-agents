"""CNPG LISTEN/NOTIFY task queue (Phase 4 substrate).

Per HOMELAB-SPEC Layer 5 task queue requirement, the substrate decision
recorded in `docs/src/task_queue_substrate_design.md` (Option B), and
the rollout plan's Phase 4.

This phase ships the primitives:
- Schema (`task_queue` + `task_dlq` tables) — `migrations/001_task_queue.sql`
- `TaskQueue.enqueue / dequeue / ack / to_dlq` — `store.py`

What comes later (Phase 4 continued):
- 4.M2: `/inbox` cutover from synchronous-call to enqueue + 202. DESTRUCTIVE
- 4.M3: DLQ surface + Grafana dashboard

The task envelope schema (`InboxRequest` fields) landed in 2.F (#50).
This module persists envelopes to Postgres; the worker that drains the
queue and dispatches to the graph is wired in 4.M2.
"""

from agents.queue.store import (
    TaskClaim,
    TaskQueue,
)

__all__ = [
    "TaskClaim",
    "TaskQueue",
]
