"""Tests for the /inbox Renee-allowlist gate (Phase 3.I).

Pins the HOMELAB-SPEC Layer 7 contract: Renee's intents are filtered
at /inbox; other requesters skip the check.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api import inbox
from agents.settings import get_settings


class _FakeGraph:
    """Minimal graph stand-in so /inbox doesn't try to run real nodes.

    The Renee gate fires BEFORE graph invocation, so requests that get
    past the gate just need a graph that returns a benign final state.
    """

    async def ainvoke(self, state: Any, config: Any) -> dict[str, Any]:
        return {"output": "ok"}

    async def aget_state(self, config: Any) -> Any:
        class _S:
            tasks: tuple = ()

        return _S()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.graph = _FakeGraph()
    yield


def _make_app() -> FastAPI:
    get_settings.cache_clear()  # type: ignore[attr-defined]
    app = FastAPI(lifespan=_lifespan)
    app.include_router(inbox.router)
    return app


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "t-001",
        "source": "test",
        "content": "hi",
    }
    base.update(overrides)
    return base


def test_renee_with_allowed_intent_passes() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(user="renee", requester="renee", intent="action"),
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"


def test_renee_with_disallowed_intent_403() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(user="renee", requester="renee", intent="bug"),
        )
    assert resp.status_code == 403
    assert "not in Renee's allowlist" in resp.json()["detail"]


def test_renee_without_intent_422() -> None:
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(user="renee", requester="renee"),  # no `intent`
        )
    assert resp.status_code == 422
    assert "requester=renee requires `intent`" in resp.json()["detail"]


@pytest.mark.parametrize("requester", ["rob", "system", None])
def test_non_renee_requesters_skip_allowlist(requester: str | None) -> None:
    """Rob, system, and None-requester requests bypass the gate."""
    app = _make_app()
    body = _body(user="rob")
    if requester is not None:
        body["requester"] = requester
        body["intent"] = "bug"  # would fail allowlist for renee
    with TestClient(app) as client:
        resp = client.post("/inbox", json=body)
    assert resp.status_code == 200


def test_envelope_default_no_requester_skips_check() -> None:
    """Old callers (no requester field) bypass the gate entirely."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(user="rob"),  # No requester, no intent — old caller.
        )
    assert resp.status_code == 200


def test_allowlist_widening_via_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting RENEE_ALLOWED_INTENTS widens the gate."""
    monkeypatch.setenv("RENEE_ALLOWED_INTENTS", '["action","question","note"]')
    get_settings.cache_clear()  # type: ignore[attr-defined]
    app = FastAPI(lifespan=_lifespan)
    app.include_router(inbox.router)
    with TestClient(app) as client:
        resp = client.post(
            "/inbox",
            json=_body(user="renee", requester="renee", intent="note"),
        )
    assert resp.status_code == 200
