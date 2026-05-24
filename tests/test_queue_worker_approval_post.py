"""Tests for the worker-side approval-post webhook.

Phase 4.M2 worker emits an HTTP POST to Settings.approval_post_webhook_url
when the graph paused at interrupt() with an ApprovalRequest payload.
Without that, the first user-visible signal would be the 30-min
escalation tier from langgraph-awaiting-user-sweep.ts.

These tests pin the contract: webhook fires on interrupt, stays silent
when no interrupt, stays silent when URL is unset, and never raises
when the webhook itself errors.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from agents.queue.approval_post import post_approval_for_interrupts
from agents.queue.worker import QueueWorker
from agents.settings import get_settings


class _FakeInterrupt:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


class _FakeGraphTask:
    def __init__(self, interrupts: list[_FakeInterrupt]) -> None:
        self.interrupts = interrupts


class _FakeSnapshot:
    def __init__(self, tasks: list[_FakeGraphTask]) -> None:
        self.tasks = tasks


class _FakeGraph:
    def __init__(self, snapshot: _FakeSnapshot) -> None:
        self._snapshot = snapshot
        self.calls: list[dict[str, Any]] = []

    async def aget_state(self, config: dict[str, Any]) -> _FakeSnapshot:
        self.calls.append(config)
        return self._snapshot


def _build_worker(graph: _FakeGraph) -> QueueWorker:
    """Minimal QueueWorker — only the approval-post path needs real wiring."""
    return QueueWorker(
        queue=MagicMock(),
        pool=MagicMock(),
        graph=graph,
    )


def _approval_request_dict() -> dict[str, Any]:
    return {
        "action_class": "C",
        "target": "ml-operator.deploy_model",
        "payload_summary": "Roll out qwen2.5-coder:32b on Spark",
        "undo_path": "kubectl rollout undo deploy/ollama-spark",
        "proposed_by": "ml-operator",
        "cost_estimate_usd": 0.0,
    }


def _set_webhook(
    monkeypatch: pytest.MonkeyPatch,
    url: str | None,
    token: str | None = None,
) -> None:
    if url is None:
        monkeypatch.delenv("APPROVAL_POST_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("APPROVAL_POST_WEBHOOK_URL", url)
    if token is None:
        monkeypatch.delenv("APPROVAL_POST_WEBHOOK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("APPROVAL_POST_WEBHOOK_TOKEN", token)
    get_settings.cache_clear()


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _PostRecorder:
    """Drop-in replacement for httpx.AsyncClient that captures POSTs."""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self._status_code = status_code
        self._text = text
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.raise_on_post = False

    def __call__(self, **_kwargs: Any) -> _PostRecorder:  # AsyncClient(timeout=...)
        return self

    async def __aenter__(self) -> _PostRecorder:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls.append((url, json, dict(headers or {})))
        if self.raise_on_post:
            raise httpx.ConnectError("simulated")
        return _FakeResponse(self._status_code, self._text)


def test_approval_post_fires_when_interrupt_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot with an ApprovalRequest-shaped interrupt → POST happens."""
    _set_webhook(monkeypatch, "http://windmill.example/api/approval-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot([_FakeGraphTask([_FakeInterrupt(_approval_request_dict())])])
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))

    assert len(recorder.calls) == 1
    url, body, headers = recorder.calls[0]
    assert url == "http://windmill.example/api/approval-post"
    assert body["task_id"] == "01J-task"
    # Windmill convention: function args == top-level body keys.
    # The receiving script expects `paused_for: {approval_request: ...}`.
    assert body["paused_for"]["approval_request"]["action_class"] == "C"
    assert body["paused_for"]["approval_request"]["proposed_by"] == "ml-operator"
    # No token configured in this test → no Authorization header.
    assert "Authorization" not in headers


def test_approval_post_sends_bearer_token_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When APPROVAL_POST_WEBHOOK_TOKEN is set, POST carries Authorization."""
    _set_webhook(
        monkeypatch,
        "http://windmill.example/api/approval-post",
        token="windmill-secret-token",
    )
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot([_FakeGraphTask([_FakeInterrupt(_approval_request_dict())])])
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))

    assert len(recorder.calls) == 1
    _url, _body, headers = recorder.calls[0]
    assert headers["Authorization"] == "Bearer windmill-secret-token"


def test_approval_post_silent_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default settings have webhook_url=None → no POST, no aget_state call."""
    _set_webhook(monkeypatch, None)
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot([_FakeGraphTask([_FakeInterrupt(_approval_request_dict())])])
    graph = _FakeGraph(snapshot)
    worker = _build_worker(graph)

    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))

    assert recorder.calls == []
    # aget_state should not have been called either — short-circuited at the URL check.
    assert graph.calls == []


def test_approval_post_silent_when_no_interrupts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Graph completed normally → no interrupts, no POST."""
    _set_webhook(monkeypatch, "http://windmill.example/api/approval-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot([_FakeGraphTask([])])  # task present, no interrupts
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))

    assert recorder.calls == []


def test_approval_post_silent_for_non_approval_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt without `action_class` (some future shape) is ignored."""
    _set_webhook(monkeypatch, "http://windmill.example/api/approval-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot([_FakeGraphTask([_FakeInterrupt({"some_other_shape": True})])])
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))

    assert recorder.calls == []


def test_approval_post_does_not_raise_on_webhook_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failure during POST is logged but does not propagate."""
    _set_webhook(monkeypatch, "http://windmill.example/api/approval-post")
    recorder = _PostRecorder()
    recorder.raise_on_post = True
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot([_FakeGraphTask([_FakeInterrupt(_approval_request_dict())])])
    worker = _build_worker(_FakeGraph(snapshot))

    # Should NOT raise.
    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))


def test_approval_post_does_not_raise_on_aget_state_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aget_state failure is logged but does not propagate."""
    _set_webhook(monkeypatch, "http://windmill.example/api/approval-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    class _ExplodingGraph:
        async def aget_state(self, _config: dict[str, Any]) -> Any:
            raise RuntimeError("checkpointer down")

    worker = _build_worker(_ExplodingGraph())  # type: ignore[arg-type]

    # Should NOT raise.
    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="test prompt"))

    # And no POST should have happened.
    assert recorder.calls == []
