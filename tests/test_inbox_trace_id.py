"""Tests for trace_id minting at /inbox ingress.

HOMELAB-SPEC Layer 5: every task carries a `trace_id` that follows it
from ingress through every mode hop. Most callers don't supply one, so
`/inbox` mints it at the single ingress point — giving the task a
stable id for its whole life in the queue, worker, graph, and webhook
payloads.

These tests pin: a supplied trace_id is preserved; an absent one is
minted as a 32-hex (128-bit) id; the minted id lands in the enqueued
envelope so the worker + downstream hops inherit it.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api import inbox
from agents.api.inbox import InboxRequest, _ensure_trace_id
from agents.settings import get_settings

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")


class _CapturingQueue:
    """Stand-in for TaskQueue that records the enqueued envelope."""

    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    async def enqueue(self, envelope: dict[str, Any]) -> str:
        self.envelopes.append(envelope)
        return "fake-task-id-123"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.task_queue = _CapturingQueue()
    yield


def _make_app() -> FastAPI:
    get_settings.cache_clear()  # type: ignore[attr-defined]
    app = FastAPI(lifespan=_lifespan)
    app.include_router(inbox.router)
    return app


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"task_id": "t-001", "source": "test", "content": "hi"}
    base.update(overrides)
    return base


# ---------- _ensure_trace_id unit ----------


def test_ensure_trace_id_preserves_supplied() -> None:
    """A caller-supplied trace_id is never overwritten."""
    req = InboxRequest(task_id="t", source="test", content="hi", trace_id="caller-supplied-id")
    _ensure_trace_id(req)
    assert req.trace_id == "caller-supplied-id"


def test_ensure_trace_id_mints_when_absent() -> None:
    """No span active (unit context) → fall back to a fresh 32-hex id."""
    req = InboxRequest(task_id="t", source="test", content="hi")
    assert req.trace_id is None
    _ensure_trace_id(req)
    assert req.trace_id is not None
    assert _HEX32.match(req.trace_id)


# ---------- /inbox ingress ----------


def test_inbox_mints_trace_id_into_envelope() -> None:
    """A request without trace_id gets one minted into the enqueued envelope."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body())
    assert resp.status_code == 202
    queue: _CapturingQueue = app.state.task_queue
    assert len(queue.envelopes) == 1
    trace_id = queue.envelopes[0]["trace_id"]
    assert trace_id is not None
    assert _HEX32.match(trace_id)


def test_inbox_preserves_supplied_trace_id() -> None:
    """A caller that supplies trace_id keeps it through to the envelope."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(trace_id="upstream-trace-abc"))
    assert resp.status_code == 202
    queue: _CapturingQueue = app.state.task_queue
    assert queue.envelopes[0]["trace_id"] == "upstream-trace-abc"
