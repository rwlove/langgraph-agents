"""Tests for the task-contract validator at /inbox ingress.

HOMELAB-SPEC Layer 5 says "tasks without this envelope are rejected at
ingress." `/inbox` runs `_validate_envelope` in *minimal-mandatory*
(lenient) mode: it enforces only the semantic checks Pydantic can't
express on its own, and lets every optional envelope field ride on its
default. The point is zero blast radius on current traffic — no live
caller populates the full envelope — while still slamming the door on
genuinely meaningless tasks.

These tests pin both halves:

- **Reject (422 + reason)** — one per semantic criterion the validator
  owns: blank `task_id`, blank `content`, non-positive `ttl_seconds`,
  blank `idempotency_key`, unknown `target_agent`.
- **Accept (202)** — the real caller shapes in production today, proving
  the validator never rejects live traffic. Includes `target_agent`
  values across the loose `ALL_AGENT_IDS` set (`historian`,
  `storage-operator`, `reviewer`) as a regression guard against anyone
  tightening it to a routing-target subset.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api import inbox
from agents.settings import get_settings


class _CapturingQueue:
    """Stand-in for TaskQueue that records the enqueued envelope."""

    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    async def enqueue(self, envelope: dict[str, Any]) -> str:
        self.envelopes.append(envelope)
        return "fake-task-id-123"


class _DedupStub:
    """Dedup store that never reports a prior task (cache miss every time)."""

    async def check_and_set(self, *, key: str, value: str) -> str | None:
        return None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.task_queue = _CapturingQueue()
    app.state.dedup_store = _DedupStub()
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


# ---------- reject matrix (422 + reason) ----------


def test_reject_blank_task_id() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(task_id="   "))
    assert resp.status_code == 422
    assert "task_id" in resp.json()["detail"]
    assert app.state.task_queue.envelopes == []


def test_reject_blank_content() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(content=""))
    assert resp.status_code == 422
    assert "content" in resp.json()["detail"]
    assert app.state.task_queue.envelopes == []


def test_reject_ttl_zero() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(ttl_seconds=0))
    assert resp.status_code == 422
    assert "ttl_seconds" in resp.json()["detail"]
    assert app.state.task_queue.envelopes == []


def test_reject_ttl_negative() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(ttl_seconds=-1))
    assert resp.status_code == 422
    assert "ttl_seconds" in resp.json()["detail"]
    assert app.state.task_queue.envelopes == []


def test_reject_blank_idempotency_key() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(idempotency_key="  "))
    assert resp.status_code == 422
    assert "idempotency_key" in resp.json()["detail"]
    assert app.state.task_queue.envelopes == []


def test_reject_unknown_target_agent() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(target_agent="not-an-agent"))
    assert resp.status_code == 422
    assert "target_agent" in resp.json()["detail"]
    assert app.state.task_queue.envelopes == []


# ---------- accept matrix (202) — live caller shapes ----------


def test_accept_central_pipe() -> None:
    """The central langgraph-inbox pipe: task_id/source/content/user."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(source="cli", user="rob"))
    assert resp.status_code == 202
    assert len(app.state.task_queue.envelopes) == 1


def test_accept_scheduled_with_pin() -> None:
    """A scheduled Windmill flow that pins a specialist via target_agent."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(source="scheduled", target_agent="historian"),
        )
    assert resp.status_code == 202
    assert len(app.state.task_queue.envelopes) == 1


def test_accept_holmesgpt_shape() -> None:
    """HolmesGPT: pinned specialist + idempotency_key + priority."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(
                source="holmesgpt",
                target_agent="storage-operator",
                idempotency_key="holmes-abc-123",
                priority="high",
            ),
        )
    assert resp.status_code == 202
    assert len(app.state.task_queue.envelopes) == 1


def test_accept_zulip_shape() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/inbox", json=_body(source="zulip"))
    assert resp.status_code == 202
    assert len(app.state.task_queue.envelopes) == 1


def test_accept_reviewer_target_agent() -> None:
    """Regression guard: `reviewer` is a live pin (langgraph-reviewer-weekly).

    The validator must check against the full ALL_AGENT_IDS set, not a
    routing-target subset — a specialists-only check would 422 this.
    """
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(source="scheduled", target_agent="reviewer"),
        )
    assert resp.status_code == 202
    assert len(app.state.task_queue.envelopes) == 1
