"""OpenAI-compatible /v1/* endpoints for OpenWebUI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.api import chat_completions
from agents.personas import invalidate_cache
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS


def _make_app(temp_vault: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(chat_completions.router)
    return app


def test_list_models_returns_17_agents(temp_vault: Path) -> None:
    app = _make_app(temp_vault)
    with TestClient(app) as client:
        r = client.get("/v1/models")
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        ids = [m["id"] for m in body["data"]]
        assert set(ids) == set(ALL_AGENT_IDS)
        assert len(ids) == 17  # +observability-operator


def test_chat_rejects_unknown_model(temp_vault: Path) -> None:
    app = _make_app(temp_vault)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "not-an-agent",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r.status_code == 404


def test_chat_rejects_empty_messages(temp_vault: Path) -> None:
    app = _make_app(temp_vault)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "triager", "messages": []},
        )
        assert r.status_code == 400


def test_chat_non_streaming_happy_path(temp_vault: Path) -> None:
    """Mock the LLM; verify the OpenAI-shape response."""
    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(return_value=MagicMock(content="hello world"))

    app = _make_app(temp_vault)
    with patch.object(chat_completions, "_make_llm", return_value=fake_llm):
        with TestClient(app) as client:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": "triager",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "triager"
    assert body["choices"][0]["message"]["content"] == "hello world"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["object"] == "chat.completion"


def test_chat_503_when_persona_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the persona files haven't been synced to the vault yet, fail with a
    clear 503 rather than a generic 500."""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))  # empty vault
    get_settings.cache_clear()
    invalidate_cache()

    app = _make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "triager", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503
    assert "persona" in r.json()["detail"]
