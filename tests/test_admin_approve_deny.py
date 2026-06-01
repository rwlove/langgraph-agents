"""Tests for the HA-Companion approve/deny admin endpoints.

`POST /admin/tasks/{id}/approve` and `/deny` are the server-side half of
the phone-native approvals flow (HomeAIOps Gate-2 slice 3). They resume a
paused approval interrupt with a verdict — the same `Command(resume=...)`
contract `api/approval.py` uses for the ntfy buttons — but mint the HMAC
approval token HERE rather than receiving it from Windmill, so the phone
only needs its existing `/admin` bearer.

The load-bearing assertion (`test_minted_token_passes_real_verifier`)
round-trips a minted token through errand-runner's REAL
`_verify_approval_token`, so the in-cluster signer can't silently drift
from the verifier it has to satisfy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api import admin
from agents.api.admin import _sign_approval_token, router
from agents.nodes.errand_runner import _verify_approval_token

_SECRET = "test-signing-secret"


class _FakeQueue:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str | None]] = []
        self.parked: list[tuple[str, datetime]] = []

    async def resolve_approval(self, task_id: str, output: str | None) -> None:
        self.resolved.append((task_id, output))

    async def park_for_approval(self, task_id: str, deadline: datetime) -> None:
        self.parked.append((task_id, deadline))


class _FakeSettings:
    langgraph_approval_signing_key: str | None = _SECRET
    approval_ttl_seconds: int = 3600


def _paused_snapshot(*, action_class: str = "C", target: str = "ha-mcp.call_service") -> MagicMock:
    """A snapshot paused at an approval interrupt whose value is a real dict.

    The endpoint reads `interrupts[0].value` (the ApprovalRequest dump) to
    learn `action_class` + `target`, so `.value` must be a real mapping,
    not a MagicMock.
    """
    snap = MagicMock()
    interrupt = MagicMock()
    interrupt.value = {"action_class": action_class, "target": target}
    snap.tasks = [MagicMock()]
    snap.tasks[0].interrupts = [interrupt]
    return snap


def _terminal_snapshot() -> MagicMock:
    snap = MagicMock()
    snap.tasks = []
    return snap


def _build_app(graph: Any, queue: Any = None, *, smoke_graph: Any = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.graph = graph
    app.state.smoke_graph = smoke_graph
    app.state.task_queue = queue
    app.state.hai_cli_token = None  # middleware not mounted in these unit apps
    return app


# --- token signer / verifier agreement (the load-bearing assertion) ---


def test_minted_token_passes_real_verifier() -> None:
    """A token this module mints must satisfy errand-runner's verifier."""
    token = _sign_approval_token("01J-task", "C", "ha-mcp", "call_service", signing_secret=_SECRET)
    assert _verify_approval_token(
        token,
        task_id="01J-task",
        action_class="C",
        server="ha-mcp",
        method="call_service",
        signing_secret=_SECRET,
    )


def test_minted_token_rejected_under_wrong_secret() -> None:
    token = _sign_approval_token("01J-task", "C", "ha-mcp", "call_service", signing_secret=_SECRET)
    assert not _verify_approval_token(
        token,
        task_id="01J-task",
        action_class="C",
        server="ha-mcp",
        method="call_service",
        signing_secret="some-other-secret",
    )


def test_minted_token_bound_to_task_fields() -> None:
    """The verifier rejects a token replayed against a different task/target."""
    token = _sign_approval_token("01J-task", "C", "ha-mcp", "call_service", signing_secret=_SECRET)
    assert not _verify_approval_token(
        token,
        task_id="01J-OTHER",
        action_class="C",
        server="ha-mcp",
        method="call_service",
        signing_secret=_SECRET,
    )


def test_nonce_is_random_per_mint() -> None:
    a = _sign_approval_token("t", "C", "s", "m", signing_secret=_SECRET)
    b = _sign_approval_token("t", "C", "s", "m", signing_secret=_SECRET)
    assert a != b  # fresh nonce each time


# --- approve endpoint ---


def test_approve_mints_token_and_resolves_done(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _terminal_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": "executed"})
    queue = _FakeQueue()
    client = TestClient(_build_app(graph, queue))

    r = client.post("/admin/tasks/01J-prod/approve", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "complete"
    assert queue.resolved == [("01J-prod", "executed")]
    assert queue.parked == []

    # The resume carried a granted verdict with a real signed token whose
    # fields match the interrupt, so errand-runner would accept it.
    resume_cmd = graph.ainvoke.call_args.args[0]
    resume_value = resume_cmd.resume
    assert resume_value["granted"] is True
    assert _verify_approval_token(
        resume_value["approval_token"],
        task_id="01J-prod",
        action_class="C",
        server="ha-mcp",
        method="call_service",
        signing_secret=_SECRET,
    )


def test_approve_two_person_reparks(monkeypatch: Any) -> None:
    """A still-paused post-resume state (two-person re-interrupt) re-parks."""
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _paused_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": None})
    queue = _FakeQueue()
    client = TestClient(_build_app(graph, queue))

    r = client.post("/admin/tasks/01J-nsdelete/approve", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "resumed"
    assert queue.resolved == []
    assert len(queue.parked) == 1
    assert queue.parked[0][0] == "01J-nsdelete"


def test_approve_actor_threaded_through(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _terminal_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    client = TestClient(_build_app(graph, _FakeQueue()))

    r = client.post("/admin/tasks/01J-prod/approve", json={"actor": "renee"})
    assert r.status_code == 200
    assert graph.ainvoke.call_args.args[0].resume["actor"] == "renee"


def test_approve_defaults_actor_rob_on_empty_body(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _terminal_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    client = TestClient(_build_app(graph, _FakeQueue()))

    # No body at all — actor should default to "rob".
    r = client.post("/admin/tasks/01J-prod/approve")
    assert r.status_code == 200
    assert graph.ainvoke.call_args.args[0].resume["actor"] == "rob"


# --- deny endpoint ---


def test_deny_sends_rejected_verdict_no_token(monkeypatch: Any) -> None:
    """Deny resumes with granted=False and an empty token (errand-runner
    refuses before it verifies a token)."""
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _terminal_snapshot()])
    graph.ainvoke = AsyncMock(
        return_value={"output": "errand-runner: approval not granted; refusing."}
    )
    queue = _FakeQueue()
    client = TestClient(_build_app(graph, queue))

    r = client.post("/admin/tasks/01J-prod/deny", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "complete"
    resume_value = graph.ainvoke.call_args.args[0].resume
    assert resume_value["granted"] is False
    assert resume_value["approval_token"] == ""
    assert queue.resolved == [("01J-prod", "errand-runner: approval not granted; refusing.")]


# --- guards ---


def test_approve_409_when_not_paused(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_terminal_snapshot())
    client = TestClient(_build_app(graph, _FakeQueue()))

    r = client.post("/admin/tasks/01J-prod/approve", json={})
    assert r.status_code == 409
    graph.ainvoke.assert_not_called()


def test_approve_503_when_graph_uninitialized(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    client = TestClient(_build_app(None, _FakeQueue()))
    r = client.post("/admin/tasks/01J-prod/approve", json={})
    assert r.status_code == 503
    assert "fleet_graph not initialized" in r.json()["detail"]


def test_approve_503_when_signing_key_missing(monkeypatch: Any) -> None:
    class _NoKey:
        langgraph_approval_signing_key = None
        approval_ttl_seconds = 3600

    monkeypatch.setattr(admin, "get_settings", _NoKey)
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_paused_snapshot())
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    client = TestClient(_build_app(graph, _FakeQueue()))

    r = client.post("/admin/tasks/01J-prod/approve", json={})
    assert r.status_code == 503
    assert "signing_key" in r.json()["detail"]
    graph.ainvoke.assert_not_called()


def test_approve_422_when_interrupt_missing_target(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    graph = MagicMock()
    # action_class present but target has no "server.method" dot.
    graph.aget_state = AsyncMock(
        return_value=_paused_snapshot(action_class="C", target="malformed")
    )
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    client = TestClient(_build_app(graph, _FakeQueue()))

    r = client.post("/admin/tasks/01J-prod/approve", json={})
    assert r.status_code == 422
    graph.ainvoke.assert_not_called()


def test_deny_does_not_require_signing_key(monkeypatch: Any) -> None:
    """Deny never mints a token, so a missing signing key must not block it."""

    class _NoKey:
        langgraph_approval_signing_key = None
        approval_ttl_seconds = 3600

    monkeypatch.setattr(admin, "get_settings", _NoKey)
    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=[_paused_snapshot(), _terminal_snapshot()])
    graph.ainvoke = AsyncMock(return_value={"output": "refused"})
    client = TestClient(_build_app(graph, _FakeQueue()))

    r = client.post("/admin/tasks/01J-prod/deny", json={})
    assert r.status_code == 200


def test_smoke_task_routes_to_smoke_graph(monkeypatch: Any) -> None:
    monkeypatch.setattr(admin, "get_settings", _FakeSettings)
    fleet = MagicMock()
    smoke = MagicMock()
    smoke.aget_state = AsyncMock(return_value=_paused_snapshot())
    smoke.ainvoke = AsyncMock(return_value={"output": "ok"})
    client = TestClient(_build_app(fleet, _FakeQueue(), smoke_graph=smoke))

    r = client.post("/admin/tasks/smoke-foo/approve", json={})
    assert r.status_code == 200
    smoke.ainvoke.assert_called_once()
    fleet.ainvoke.assert_not_called()
    # smoke tasks skip queue reconcile → single aget_state call
    assert smoke.aget_state.call_count == 1
