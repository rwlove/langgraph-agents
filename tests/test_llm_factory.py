"""Tests for `agents.llm` — per-agent LLM factory + per-group routing.

Covers:
- AGENT_GROUP covers every AgentId (drift catcher — same family as
  test_node_registry.py's NODES vs AgentId check).
- health-tracker NEVER escalates to Claude, even on explicit request.
- local-spark degrades to local-p40 when Spark is unhealthy.
- local-p40 has no Blackwell fallback (light agents → P40 only).
- LocalOllamaUnavailable carries the failed_group field for queue routing.
- escalate=True returns Claude when ANTHROPIC_API_KEY is set.
- The factory strips a legacy /v1 suffix from Ollama URLs defensively.
"""

from __future__ import annotations

from typing import get_args
from unittest.mock import patch

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama

from agents.health import reset_cache
from agents.llm import AGENT_GROUP, GROUP_MODELS, LocalOllamaUnavailable, llm
from agents.settings import get_settings
from agents.state import AgentId


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    """Ensure each test starts with no cached health state."""
    reset_cache()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Ensure settings env-overrides apply per-test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_agent_group_covers_every_agent_id() -> None:
    """AGENT_GROUP must have an entry for every AgentId Literal member.

    Same drift-protection class as test_node_registry.py — adding a new
    AgentId without also adding an AGENT_GROUP row should fail CI loudly.
    """
    expected: set[str] = set(get_args(AgentId))
    assert set(AGENT_GROUP) == expected, (
        f"AGENT_GROUP missing {expected - set(AGENT_GROUP)}, "
        f"extra {set(AGENT_GROUP) - expected}"
    )


def test_group_models_has_all_groups() -> None:
    """GROUP_MODELS must declare a model for every group used by AGENT_GROUP."""
    used_groups = set(AGENT_GROUP.values())
    assert used_groups.issubset(set(GROUP_MODELS)), (
        f"AGENT_GROUP references groups missing from GROUP_MODELS: "
        f"{used_groups - set(GROUP_MODELS)}"
    )


def test_local_spark_returns_chat_ollama_when_spark_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder")  # coder is local-spark
    assert isinstance(model, ChatOllama)
    assert "spark.test" in model.base_url
    assert model.model == "qwen2.5:32b"


def test_local_p40_returns_chat_ollama_when_p40_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")  # triager is local-p40
    assert isinstance(model, ChatOllama)
    assert "p40.test" in model.base_url
    assert model.model == "qwen2.5:7b"


def test_local_spark_degrades_to_p40_when_spark_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spark down → heavy-agent call routes to P40 (degraded quality, not down)."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")

    def _health(url: str, *, ttl_seconds: float = 60.0) -> bool:
        return "p40.test" in url  # P40 up, Spark down

    with patch("agents.llm.service_healthy", side_effect=_health):
        model = llm("coder")
    assert isinstance(model, ChatOllama)
    assert "p40.test" in model.base_url
    assert model.model == "qwen2.5:7b"  # the degraded model, NOT the original 32b


def test_local_p40_has_no_blackwell_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """P40 down → light-agent call raises (not degrade up to Spark — that'd waste GPU)."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch("agents.llm.service_healthy", return_value=False), pytest.raises(
        LocalOllamaUnavailable
    ) as excinfo:
        llm("triager")

    assert excinfo.value.failed_group == "local-p40"
    assert excinfo.value.agent_id == "triager"
    assert excinfo.value.group == "local-p40"


def test_local_spark_raises_with_failed_group_local_spark_when_both_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both local paths down → raise with failed_group=local-spark (the original group)."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch("agents.llm.service_healthy", return_value=False), pytest.raises(
        LocalOllamaUnavailable
    ) as excinfo:
        llm("coder")  # coder is local-spark
    assert excinfo.value.group == "local-spark"
    assert excinfo.value.failed_group == "local-spark"


def test_health_tracker_never_escalates_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard constraint: health-tracker is local-only; explicit escalate is downgraded."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("health-tracker", escalate=True)
    assert isinstance(model, ChatOllama)  # NOT ChatAnthropic
    assert model.model == "qwen2.5:7b"


def test_health_tracker_rejects_claude_group_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """group_override=claude on health-tracker is downgraded to local-p40."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("health-tracker", group_override="claude")
    assert isinstance(model, ChatOllama)


def test_escalate_true_returns_claude_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
    # service_healthy doesn't matter — escalate short-circuits
    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)
    assert isinstance(model, ChatAnthropic)


def test_degraded_mode_escalation_routes_to_claude_when_both_local_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DEGRADED_MODE_ESCALATION_ENABLED", "true")

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder")
    assert isinstance(model, ChatAnthropic)


def test_factory_strips_legacy_v1_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """ChatOllama uses /api routes, not /v1. Strip a leaked /v1 suffix."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434/v1")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")
    assert isinstance(model, ChatOllama)
    assert model.base_url == "http://p40.test:11434"
    assert "/v1" not in model.base_url
