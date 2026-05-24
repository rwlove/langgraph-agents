"""Verify /approval routes smoke task_ids to smoke_graph, others to fleet."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api.approval import router


def _build_app(*, fleet_graph: Any = None, smoke_graph: Any = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.graph = fleet_graph
    app.state.smoke_graph = smoke_graph
    return app


def _make_paused_graph() -> MagicMock:
    """A graph mock that reports paused-at-interrupt and resumes happily."""
    graph = MagicMock()
    snapshot = MagicMock()
    snapshot.tasks = [MagicMock()]
    snapshot.tasks[0].interrupts = [MagicMock()]
    graph.aget_state = AsyncMock(return_value=snapshot)
    graph.ainvoke = AsyncMock(return_value={"output": "ok"})
    return graph


def test_smoke_task_id_routes_to_smoke_graph() -> None:
    """A task_id starting with `smoke-` resumes on smoke_graph."""
    fleet = _make_paused_graph()
    smoke = _make_paused_graph()
    client = TestClient(_build_app(fleet_graph=fleet, smoke_graph=smoke))

    r = client.post(
        "/approval",
        json={
            "task_id": "smoke-verify-20260524T005543",
            "reaction": "approve",
            "approval_token": "any-token",
        },
    )
    assert r.status_code == 200
    # ONLY the smoke graph saw the resume
    smoke.aget_state.assert_called_once()
    smoke.ainvoke.assert_called_once()
    fleet.aget_state.assert_not_called()
    fleet.ainvoke.assert_not_called()


def test_non_smoke_task_id_routes_to_fleet_graph() -> None:
    """A normal production task_id (e.g. ULID) resumes on fleet_graph."""
    fleet = _make_paused_graph()
    smoke = _make_paused_graph()
    client = TestClient(_build_app(fleet_graph=fleet, smoke_graph=smoke))

    r = client.post(
        "/approval",
        json={
            "task_id": "01J5MRX8K3T8Q5V4F7G9HZP2Y6",
            "reaction": "approve",
            "approval_token": "any-token",
        },
    )
    assert r.status_code == 200
    fleet.aget_state.assert_called_once()
    fleet.ainvoke.assert_called_once()
    smoke.aget_state.assert_not_called()
    smoke.ainvoke.assert_not_called()


def test_smoke_with_unset_smoke_graph_503s() -> None:
    """If smoke_graph is missing in app.state, /approval should fail loudly
    instead of silently falling back to fleet_graph (which would mis-route)."""
    fleet = _make_paused_graph()
    client = TestClient(_build_app(fleet_graph=fleet, smoke_graph=None))

    r = client.post(
        "/approval",
        json={
            "task_id": "smoke-anything",
            "reaction": "approve",
            "approval_token": "any-token",
        },
    )
    assert r.status_code == 503
    assert "smoke_graph not initialized" in r.json()["detail"]


def test_fleet_with_unset_fleet_graph_503s() -> None:
    """Symmetric guarantee for the production path."""
    smoke = _make_paused_graph()
    client = TestClient(_build_app(fleet_graph=None, smoke_graph=smoke))

    r = client.post(
        "/approval",
        json={
            "task_id": "01J5MRX8K3T8Q5V4F7G9HZP2Y6",
            "reaction": "approve",
            "approval_token": "any-token",
        },
    )
    assert r.status_code == 503
    assert "fleet_graph not initialized" in r.json()["detail"]


@pytest.mark.parametrize(
    "task_id,expected_graph",
    [
        ("smoke-foo", "smoke"),
        ("smoke-", "smoke"),  # bare prefix still routes to smoke
        ("smokey-bear", "fleet"),  # only `smoke-` (with hyphen) prefix matches
        ("not-smoke-prefix", "fleet"),
        ("01J5MRX8K3T8Q5V4F7G9HZP2Y6", "fleet"),
    ],
)
def test_prefix_match_is_exact(task_id: str, expected_graph: str) -> None:
    """The discriminator is `startswith("smoke-")`, not a substring or fuzzy."""
    fleet = _make_paused_graph()
    smoke = _make_paused_graph()
    client = TestClient(_build_app(fleet_graph=fleet, smoke_graph=smoke))

    r = client.post(
        "/approval",
        json={
            "task_id": task_id,
            "reaction": "approve",
            "approval_token": "any-token",
        },
    )
    assert r.status_code == 200
    if expected_graph == "smoke":
        smoke.ainvoke.assert_called_once()
        fleet.ainvoke.assert_not_called()
    else:
        fleet.ainvoke.assert_called_once()
        smoke.ainvoke.assert_not_called()
