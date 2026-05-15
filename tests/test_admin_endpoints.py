"""Admin endpoints used by the n8n awaiting-user-sweep + cost-cap-watcher."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api import admin
from agents.state import ALL_AGENT_IDS


def _make_app_with_graph(temp_vault: Path) -> FastAPI:
    """Build a FastAPI app with the admin router + a mock graph attribute."""
    app = FastAPI()
    app.include_router(admin.router)
    app.state.graph = None  # set per-test via fixture
    return app


def test_list_agents_returns_all_fleet_ids(temp_vault: Path) -> None:
    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/agents")
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert set(ids) == set(ALL_AGENT_IDS)


def test_timeout_tier_rejects_invalid_tier(temp_vault: Path) -> None:
    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.post("/admin/tasks/t-1/timeout-tier", json={"tier": "bogus"})
        assert r.status_code == 422  # Pydantic Literal rejection


def test_timeout_tier_404_when_task_unknown(temp_vault: Path) -> None:
    """get_state on an unknown thread returns an empty StateSnapshot;
    the endpoint should 404."""
    fake_graph = MagicMock()
    snap = MagicMock()
    snap.values = None  # signal: thread not found
    fake_graph.aget_state = AsyncMock(return_value=snap)

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.post(
            "/admin/tasks/unknown-task/timeout-tier", json={"tier": "30min"}
        )
        assert r.status_code == 404


def test_cancel_404_when_task_unknown(temp_vault: Path) -> None:
    fake_graph = MagicMock()
    snap = MagicMock()
    snap.values = None
    fake_graph.aget_state = AsyncMock(return_value=snap)

    app = _make_app_with_graph(temp_vault)
    app.state.graph = fake_graph
    with TestClient(app) as client:
        r = client.post("/admin/tasks/unknown-task/cancel", json={"reason": "test"})
        assert r.status_code == 404


def test_costs_today_returns_zero_when_no_log(temp_vault: Path) -> None:
    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/costs/today")
        assert r.status_code == 200
        body = r.json()
        assert body["spent_usd"] == 0.0
        assert body["daily_cap_usd"] == 30.0  # default cap
        assert body["per_agent"] == {}
        assert "date" in body


def test_costs_today_aggregates_from_log(temp_vault: Path) -> None:
    """Plant a cost log and verify the endpoint sums it."""
    today = datetime.now(UTC).date().isoformat()
    cost_dir = temp_vault / "reports" / "costs"
    cost_dir.mkdir(parents=True)
    (cost_dir / f"{today}.jsonl").write_text(
        '{"agent": "coder", "cost_usd": 1.5}\n'
        '{"agent": "coder", "cost_usd": 0.5}\n'
        '{"agent": "homelab-engineer", "cost_usd": 2.0}\n'
    )

    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/costs/today")
        assert r.status_code == 200
        body = r.json()
        assert body["spent_usd"] == 4.0
        assert body["per_agent"] == {"coder": 2.0, "homelab-engineer": 2.0}


def test_costs_today_tolerates_malformed_log(temp_vault: Path) -> None:
    """Bad JSON line shouldn't crash the endpoint."""
    today = datetime.now(UTC).date().isoformat()
    cost_dir = temp_vault / "reports" / "costs"
    cost_dir.mkdir(parents=True)
    (cost_dir / f"{today}.jsonl").write_text(
        '{"agent": "coder", "cost_usd": 1.0}\n'
        'this is not json\n'
    )

    app = _make_app_with_graph(temp_vault)
    with TestClient(app) as client:
        r = client.get("/admin/costs/today")
        # The endpoint catches the ValueError and returns zeros (graceful degrade)
        assert r.status_code == 200
