"""Smoke-test graph for the errand-runner approval flow.

A minimal graph — `START → errand-runner → END` — that exercises the
production approval-interrupt + HMAC-verify path end-to-end without
involving any LLM-driven agent. The fleet graph normally enters at
`triager`, which would itself need to converge on errand-runner for
this test; the smoke graph removes that variance.

Triggered by `POST /admin/smoke/start-approval` (api/admin.py). The
endpoint pre-populates `state.approval_request` for the synthetic
`smoke.test_write` target before invoking this graph. The graph's
single node — errand-runner — interrupts on `approval_granted is
None`, the standard `/approval` resume path applies the verdict and
HMAC token, and on resume errand-runner runs its smoke-execution
branch (filesystem write → readback → delete with timings).

Why a separate graph vs reusing the fleet graph: the smoke endpoint
needs deterministic entry at errand-runner. Forcing the fleet graph
through triager → supervisor → errand-runner would (a) make latency
measurements noisy and (b) couple the smoke test to whatever the
qwen2.5:7b triager happens to decide. The smoke graph is one node
deep and adds zero LLM calls.
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore

from agents.nodes import NODES
from agents.state import FleetState


def build_smoke_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: BaseStore | None = None,
) -> Any:
    """Build + compile the single-node smoke graph.

    Same `compile(checkpointer=..., store=...)` contract as the fleet
    graph — the runtime passes the production checkpointer in so smoke
    runs are durably resumable via the same `/approval` endpoint that
    resumes real production interrupts.

    The smoke graph does NOT wrap the node in `_with_activity_log`
    (fleet.py's helper) — smoke runs are operational tests, not real
    agent work, and we don't want them polluting the per-agent activity
    log.
    """
    builder = StateGraph(FleetState)
    builder.add_node("errand-runner", cast(Any, NODES["errand-runner"]))
    builder.add_edge(START, "errand-runner")
    builder.add_edge("errand-runner", END)
    return builder.compile(checkpointer=checkpointer, store=store)
