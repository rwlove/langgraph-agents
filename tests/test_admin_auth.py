"""Tests for the dual-auth middleware on /admin/* and /inbox."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api.auth import hai_cli_auth_middleware


def _make_app(token: str | None) -> FastAPI:
    app = FastAPI()
    app.state.hai_cli_token = token

    @app.middleware("http")
    async def _mw(request, call_next):  # type: ignore[no-untyped-def]
        return await hai_cli_auth_middleware(request, call_next)

    @app.get("/admin/probe")
    async def _probe() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/healthz")
    async def _healthz() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_unprotected_paths_skip_auth() -> None:
    """Healthz never demands auth."""
    client = TestClient(_make_app(token="t-secret"))
    r = client.get("/healthz")
    assert r.status_code == 200


def test_token_unset_allows_admin_through_with_warning() -> None:
    """Dev mode: no token configured → admin allowed (warns in logs)."""
    client = TestClient(_make_app(token=None))
    r = client.get("/admin/probe")
    assert r.status_code == 200


def test_valid_bearer_allowed() -> None:
    client = TestClient(_make_app(token="t-secret"))
    r = client.get("/admin/probe", headers={"Authorization": "Bearer t-secret"})
    assert r.status_code == 200


def test_invalid_bearer_rejected_with_403() -> None:
    """Wrong Bearer is a clearer error than silent demotion to header-auth."""
    client = TestClient(_make_app(token="t-secret"))
    r = client.get("/admin/probe", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403
    assert "invalid bearer token" in r.json()["detail"]


def test_oauth2_proxy_email_header_allowed() -> None:
    """Classic oauth2-proxy header — X-Forwarded-Email — admits."""
    client = TestClient(_make_app(token="t-secret"))
    r = client.get(
        "/admin/probe",
        headers={"X-Forwarded-Email": "admin@example.com"},
    )
    assert r.status_code == 200


def test_oauth2_proxy_v7_header_allowed() -> None:
    """Newer oauth2-proxy header — X-Auth-Request-Email — also admits."""
    client = TestClient(_make_app(token="t-secret"))
    r = client.get(
        "/admin/probe",
        headers={"X-Auth-Request-Email": "admin@example.com"},
    )
    assert r.status_code == 200


def test_empty_oauth2_proxy_header_rejected() -> None:
    """An empty X-Forwarded-Email is NOT an attestation."""
    client = TestClient(_make_app(token="t-secret"))
    r = client.get(
        "/admin/probe",
        headers={"X-Forwarded-Email": ""},
    )
    assert r.status_code == 401


def test_no_auth_at_all_rejected_with_401() -> None:
    client = TestClient(_make_app(token="t-secret"))
    r = client.get("/admin/probe")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_bearer_takes_precedence_when_both_present() -> None:
    """If a Bearer is presented, the oauth2-proxy header is NOT a fallback.

    Rationale: presenting a wrong Bearer is a clearer error signal than
    silently demoting to header-auth (which would feel like the Bearer
    "worked" but actually didn't).
    """
    client = TestClient(_make_app(token="t-secret"))
    r = client.get(
        "/admin/probe",
        headers={
            "Authorization": "Bearer wrong",
            "X-Forwarded-Email": "admin@example.com",
        },
    )
    assert r.status_code == 403  # not 200 — Bearer error is surfaced
