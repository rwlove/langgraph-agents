"""Tests for the worker-side completion-post webhook.

Stage 2 of HomeAIOps stabilization: the worker POSTs a completion
card to ``Settings.completion_post_webhook_url`` when a task
transitions to ``status=done`` with output. The receiving Windmill
workflow DMs Rob with the human-readable summary.

These tests pin the contract: completion-post fires on done-with-
output, stays silent when URL unset, sends the correct payload
shape including target_agent + content + duration, and never raises
when the webhook itself errors.

Companion tests for the approval-post → content payload addition
(Stage 2: "You asked: ..." surface in the approval DM).
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
    def __init__(
        self,
        tasks: list[_FakeGraphTask] | None = None,
        values: dict[str, Any] | None = None,
    ) -> None:
        self.tasks = tasks or []
        self.values = values or {}


class _FakeGraph:
    def __init__(self, snapshot: _FakeSnapshot) -> None:
        self._snapshot = snapshot
        self.calls: list[dict[str, Any]] = []

    async def aget_state(self, config: dict[str, Any]) -> _FakeSnapshot:
        self.calls.append(config)
        return self._snapshot


def _build_worker(graph: _FakeGraph) -> QueueWorker:
    return QueueWorker(queue=MagicMock(), pool=MagicMock(), graph=graph)


def _set_completion_webhook(
    monkeypatch: pytest.MonkeyPatch,
    url: str | None,
    token: str | None = None,
) -> None:
    if url is None:
        monkeypatch.delenv("COMPLETION_POST_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("COMPLETION_POST_WEBHOOK_URL", url)
    if token is None:
        monkeypatch.delenv("COMPLETION_POST_WEBHOOK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("COMPLETION_POST_WEBHOOK_TOKEN", token)
    get_settings.cache_clear()


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _PostRecorder:
    def __init__(self, status_code: int = 200) -> None:
        self._status_code = status_code
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.raise_on_post = False

    def __call__(self, **_kwargs: Any) -> _PostRecorder:
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
        return _FakeResponse(self._status_code)


# ---------- _post_completion ----------


def test_completion_post_fires_with_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output present + URL set → POST carries task_id, content, output, target_agent, duration."""
    _set_completion_webhook(monkeypatch, "http://windmill.example/completion-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot(values={"target_agent": "homelab-engineer"})
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(
        worker._post_completion(
            "01J-task",
            {"content": "smoke test from rob's laptop"},
            "I checked everything. All good.",
            42.7,
        )
    )

    assert len(recorder.calls) == 1
    url, body, _headers = recorder.calls[0]
    assert url == "http://windmill.example/completion-post"
    assert body["task_id"] == "01J-task"
    assert body["target_agent"] == "homelab-engineer"
    assert body["content"] == "smoke test from rob's laptop"
    assert body["output"] == "I checked everything. All good."
    assert body["duration_s"] == 42.7


def test_completion_post_carries_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB-SPEC Layer 5: the completion card forwards the task's
    trace_id so the historian summary span shares the ingress id."""
    _set_completion_webhook(monkeypatch, "http://windmill.example/completion-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot(values={"target_agent": "homelab-engineer"})
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(
        worker._post_completion(
            "01J-task",
            {"content": "x", "trace_id": "abc123def456"},
            "done",
            1.0,
        )
    )

    _url, body, _headers = recorder.calls[0]
    assert body["trace_id"] == "abc123def456"


def test_completion_post_trace_id_empty_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A directly-enqueued task with no trace_id → empty string, not a crash."""
    _set_completion_webhook(monkeypatch, "http://windmill.example/completion-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot(values={"target_agent": "coder"})
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(worker._post_completion("01J-task", {"content": "x"}, "done", 1.0))

    _url, body, _headers = recorder.calls[0]
    assert body["trace_id"] == ""


def test_completion_post_silent_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No URL → no POST and no graph state read (avoids unnecessary lock contention)."""
    _set_completion_webhook(monkeypatch, None)
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    graph = _FakeGraph(_FakeSnapshot())
    worker = _build_worker(graph)

    asyncio.run(worker._post_completion("01J-task", {"content": "foo"}, "out", 1.0))

    assert recorder.calls == []
    assert graph.calls == []  # no aget_state call when URL unset


def test_completion_post_carries_bearer_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_completion_webhook(
        monkeypatch,
        "http://windmill.example/completion-post",
        token="windmill-secret",
    )
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot(values={"target_agent": "note-maker"})
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(worker._post_completion("01J-task", {"content": "x"}, "y", 1.0))

    _url, _body, headers = recorder.calls[0]
    assert headers["Authorization"] == "Bearer windmill-secret"


def test_completion_post_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flaky webhook must not crash the worker — best-effort only."""
    _set_completion_webhook(monkeypatch, "http://windmill.example/completion-post")
    recorder = _PostRecorder()
    recorder.raise_on_post = True
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot(values={"target_agent": "coder"})
    worker = _build_worker(_FakeGraph(snapshot))

    # No exception propagates.
    asyncio.run(worker._post_completion("01J-task", {"content": "x"}, "y", 1.0))


def test_completion_post_falls_back_when_snapshot_lacks_target_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing target_agent in checkpointer → empty string, payload still valid."""
    _set_completion_webhook(monkeypatch, "http://windmill.example/completion-post")
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    snapshot = _FakeSnapshot(values={})  # no target_agent
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(worker._post_completion("01J-task", {"content": "x"}, "y", 1.0))

    _url, body, _headers = recorder.calls[0]
    assert body["target_agent"] == ""


# ---------- approval-post `content` payload (Stage 2 addition) ----------


def test_approval_post_payload_includes_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 2 change: approval-post payload carries envelope.content
    so the Zulip DM card can show 'You asked: ...' — closes the
    bare-ULID-no-context UX gap."""
    monkeypatch.setenv("APPROVAL_POST_WEBHOOK_URL", "http://windmill.example/approval-post")
    get_settings.cache_clear()
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    approval = {
        "action_class": "C",
        "target": "ml-operator.deploy_model",
        "payload_summary": "Roll out qwen2.5-coder",
        "proposed_by": "ml-operator",
    }
    snapshot = _FakeSnapshot(tasks=[_FakeGraphTask([_FakeInterrupt(approval)])])
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(
        post_approval_for_interrupts(
            worker._graph,
            "01J-task",
            content="deploy the coder model",
        )
    )

    assert len(recorder.calls) == 1
    _url, body, _headers = recorder.calls[0]
    assert body["content"] == "deploy the coder model"
    # Existing shape preserved
    assert body["paused_for"]["approval_request"]["action_class"] == "C"


def test_approval_post_payload_includes_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOMELAB-SPEC Layer 5: the approval card forwards the task's
    trace_id so the Windmill approval span shares the ingress id."""
    monkeypatch.setenv("APPROVAL_POST_WEBHOOK_URL", "http://windmill.example/approval-post")
    get_settings.cache_clear()
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    approval = {"action_class": "C", "target": "ml-operator.deploy_model"}
    snapshot = _FakeSnapshot(tasks=[_FakeGraphTask([_FakeInterrupt(approval)])])
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(
        post_approval_for_interrupts(
            worker._graph,
            "01J-task",
            content="deploy the coder model",
            trace_id="trace-xyz-789",
        )
    )

    _url, body, _headers = recorder.calls[0]
    assert body["trace_id"] == "trace-xyz-789"


def test_approval_post_trace_id_empty_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke-test caller passes no trace_id → empty string in the payload."""
    monkeypatch.setenv("APPROVAL_POST_WEBHOOK_URL", "http://windmill.example/approval-post")
    get_settings.cache_clear()
    recorder = _PostRecorder()
    monkeypatch.setattr("agents.queue.worker.httpx.AsyncClient", recorder)

    approval = {"action_class": "A", "target": "errand-runner.noop"}
    snapshot = _FakeSnapshot(tasks=[_FakeGraphTask([_FakeInterrupt(approval)])])
    worker = _build_worker(_FakeGraph(snapshot))

    asyncio.run(post_approval_for_interrupts(worker._graph, "01J-task", content="x"))

    _url, body, _headers = recorder.calls[0]
    assert body["trace_id"] == ""
