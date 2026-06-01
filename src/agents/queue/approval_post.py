"""Shared helper for firing the Windmill approval-post webhook.

Two callers:

1. The queue worker (queue/worker.py) calls this after every task
   that hits an interrupt, so production agent flows fire the
   approval card immediately.

2. The smoke endpoint (api/admin.py) calls this after the smoke
   graph hits the errand-runner interrupt, so the smoke test
   exercises the same approval-card path production does. Without
   this shared helper, the smoke endpoint silently lacked the
   webhook fire — interrupts created by /admin/smoke/start-approval
   sat in the checkpointer with no user-facing notification until
   the 30-min `awaiting-user-sweep` fallback caught them. See
   GitHub PR #85 (the smoke capability landed) and PR #86 (this
   fix).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agents.observability import get_logger
from agents.settings import get_settings

logger = logging.getLogger(__name__)
slog = get_logger("queue.approval_post")


def _find_approval_request(snapshot: Any) -> dict[str, Any] | None:
    """Return the first ApprovalRequest-shaped interrupt in a state snapshot.

    An ApprovalRequest is a dict carrying ``action_class`` — the schema
    field that distinguishes it from any other interrupt payload that may
    appear in the future. Returns ``None`` when the graph isn't paused on
    an approval.
    """
    for graph_task in snapshot.tasks:
        for it in graph_task.interrupts:
            if isinstance(it.value, dict) and "action_class" in it.value:
                return dict(it.value)
    return None


async def has_pending_approval(graph: Any, task_id: str) -> bool:
    """True when ``task_id`` is paused on an ApprovalRequest interrupt.

    The queue worker calls this to decide whether to park the task at
    ``status='awaiting_approval'`` instead of acking it ``done``. Unlike
    ``post_approval_for_interrupts`` this is NOT gated on the webhook URL
    — a deployment with no approval webhook configured must still park
    paused tasks correctly rather than marking them done.

    Best-effort: on any checkpointer error this returns ``False`` so the
    worker falls through to its existing ack path. The dispatch loop must
    never crash on a detection read (ml-operator prime directive); a
    missed park is caught by ``langgraph-awaiting-user-sweep.ts`` at the
    30-min escalation tier.
    """
    config: Any = {"configurable": {"thread_id": task_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.exception("has_pending_approval: aget_state failed task=%s", task_id)
        return False
    return _find_approval_request(snapshot) is not None


# The human-decision subset of an ApprovalRequest. The full interrupt
# payload carries internal routing fields; the guardian listing only
# needs what lets Rob decide at a glance — the proposed action, its undo
# path, who proposed it, its class, cost, and whether it needs a second
# pair of eyes. Single source of truth so the worker (which persists it)
# and any future consumer agree on the shape.
APPROVAL_LISTING_FIELDS = (
    "payload_summary",
    "action_class",
    "proposed_by",
    "undo_path",
    "cost_estimate_usd",
    "requires_two_person",
)


def curate_approval_request(approval_request: dict[str, Any]) -> dict[str, Any]:
    """Project an ApprovalRequest down to the guardian-listing subset."""
    return {k: approval_request.get(k) for k in APPROVAL_LISTING_FIELDS}


async def get_pending_approval(graph: Any, task_id: str) -> dict[str, Any] | None:
    """Return the curated ApprovalRequest subset for a paused task, else None.

    Worker-facing accessor mirroring `has_pending_approval`'s
    error-swallowing contract: on any checkpointer read failure it
    returns None so the dispatch loop never crashes on a detection read
    (ml-operator prime directive). A missed park is caught by
    `langgraph-awaiting-user-sweep.ts` at the 30-min escalation tier.
    """
    config: Any = {"configurable": {"thread_id": task_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.exception("get_pending_approval: aget_state failed task=%s", task_id)
        return None
    approval_request = _find_approval_request(snapshot)
    if approval_request is None:
        return None
    return curate_approval_request(approval_request)


async def post_approval_for_interrupts(
    graph: Any,
    task_id: str,
    content: str = "",
    *,
    trace_id: str | None = None,
) -> None:
    """POST an ApprovalRequest to the Windmill approval-post webhook
    when ``graph`` has paused at an ``interrupt()`` call on ``task_id``.

    Best-effort: no exceptions propagate. The ``/admin/tasks/<id>``
    endpoint independently exposes the checkpointer's interrupts, and
    ``langgraph-awaiting-user-sweep.ts`` catches anything missed here
    at the 30-min escalation tier.

    Args:
      graph: A compiled LangGraph with the same checkpointer the
        interrupting task ran against. The queue worker passes its
        own ``self._graph``; the smoke endpoint passes
        ``app.state.smoke_graph``.
      task_id: Thread ID to read the interrupt from.
      content: Originating prompt — included in the webhook payload so
        the Zulip DM card can show "You asked: ...". May be empty for
        synthetic invocations (smoke tests).
      trace_id: The task's HOMELAB-SPEC Layer 5 trace_id, forwarded so
        the approval card and downstream Windmill spans share the
        task's ingress id. None for synthetic invocations.
    """
    settings = get_settings()
    webhook_url = settings.approval_post_webhook_url
    if not webhook_url:
        return

    config: Any = {"configurable": {"thread_id": task_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.exception("approval-post: aget_state failed task=%s", task_id)
        return

    # An ApprovalRequest is a dict carrying `action_class` (the schema
    # field that distinguishes it from any other interrupt payload that
    # may appear in the future).
    approval_request = _find_approval_request(snapshot)
    if approval_request is None:
        return

    # Windmill convention: function args == top-level body keys. The
    # receiving `langgraph-approval-post` script has signature
    # ``main(task_id, paused_for: {approval_request: ...}, content?: str)``
    # — `content` (the originating prompt) was added in the Stage 2
    # human-readable-card rewrite so the Zulip DM can show
    # "You asked: ...". Older versions of the workflow ignore the
    # extra field; new ones use it.
    payload = {
        "task_id": task_id,
        "trace_id": trace_id or "",
        "paused_for": {"approval_request": approval_request},
        "content": content or "",
    }
    # Windmill's `run/p/` endpoint requires auth. The standard pattern
    # in this cluster is `Authorization: Bearer <windmill_webhook_token>`.
    # Header is only sent when the token is configured; URL-only
    # deployments (test fixtures, future endpoints that don't need auth)
    # still work.
    headers: dict[str, str] = {}
    if settings.approval_post_webhook_token:
        headers["Authorization"] = f"Bearer {settings.approval_post_webhook_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)
    except Exception:
        logger.exception("approval-post: webhook call failed task=%s", task_id)
        return

    if resp.status_code >= 400:
        logger.warning(
            "approval-post: webhook %d task=%s body=%.200s",
            resp.status_code,
            task_id,
            resp.text,
        )
    else:
        slog.info(
            "approval_post",
            task_id=task_id,
            status=resp.status_code,
            action_class=approval_request.get("action_class"),
        )
