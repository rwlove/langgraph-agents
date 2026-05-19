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

from typing import Any, get_args
from unittest.mock import patch

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.runnables import RunnableBinding
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from agents.health import reset_cache
from agents.llm import AGENT_GROUP, GROUP_MODELS, LocalOllamaUnavailable, llm
from agents.observability import LangGraphMetricsCallback
from agents.settings import get_settings
from agents.state import AgentId


def _unwrap(model: Any) -> Any:
    """Strip the metadata+callbacks `with_config` binding to access the underlying chat model.

    The factory wraps each model in a RunnableBinding so observability metadata
    and the metrics callback flow without per-call-site plumbing. Tests that
    assert on the concrete model type/URL need to peel that wrapper off.
    """
    return model.bound if isinstance(model, RunnableBinding) else model


def _config(model: Any) -> dict[str, Any]:
    """Pull the bound RunnableConfig off a factory-returned model."""
    return model.config if isinstance(model, RunnableBinding) else {}


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
    base = _unwrap(model)
    assert isinstance(base, ChatOllama)
    assert "spark.test" in base.base_url
    assert base.model == "qwen2.5:32b"
    # Observability binding: metadata reflects effective routing
    cfg = _config(model)
    assert cfg["metadata"] == {"agent": "coder", "group": "local-spark", "model": "qwen2.5:32b"}
    assert any(isinstance(cb, LangGraphMetricsCallback) for cb in cfg["callbacks"])


def test_local_p40_returns_chat_ollama_when_p40_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")  # triager is local-p40
    base = _unwrap(model)
    assert isinstance(base, ChatOllama)
    assert "p40.test" in base.base_url
    assert base.model == "qwen2.5:7b"
    cfg = _config(model)
    assert cfg["metadata"] == {"agent": "triager", "group": "local-p40", "model": "qwen2.5:7b"}


def test_local_spark_degrades_to_p40_when_spark_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spark down → heavy-agent call routes to P40 (degraded quality, not down).

    Critically, the metric metadata reflects the EFFECTIVE group (local-p40)
    so Grafana shows the actual routing, not the requested group.
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")

    def _health(url: str, *, ttl_seconds: float = 60.0) -> bool:
        return "p40.test" in url  # P40 up, Spark down

    with patch("agents.llm.service_healthy", side_effect=_health):
        model = llm("coder")
    base = _unwrap(model)
    assert isinstance(base, ChatOllama)
    assert "p40.test" in base.base_url
    assert base.model == "qwen2.5:7b"  # the degraded model, NOT the original 32b
    cfg = _config(model)
    # Effective group is local-p40 (the one actually serving) so metric labels
    # tell the truth about the degraded routing.
    assert cfg["metadata"]["group"] == "local-p40"
    assert cfg["metadata"]["model"] == "qwen2.5:7b"
    assert cfg["metadata"]["agent"] == "coder"


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
    base = _unwrap(model)
    assert isinstance(base, ChatOllama)  # NOT ChatAnthropic
    assert base.model == "qwen2.5:7b"


def test_health_tracker_rejects_claude_group_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """group_override=claude on health-tracker is downgraded to local-p40."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("health-tracker", group_override="claude")
    base = _unwrap(model)
    assert isinstance(base, ChatOllama)


def test_escalate_true_returns_claude_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
    # service_healthy doesn't matter — escalate short-circuits
    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)
    base = _unwrap(model)
    assert isinstance(base, ChatAnthropic)


def test_degraded_mode_escalation_routes_to_claude_when_both_local_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DEGRADED_MODE_ESCALATION_ENABLED", "true")

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder")
    base = _unwrap(model)
    assert isinstance(base, ChatAnthropic)


def test_factory_strips_legacy_v1_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """ChatOllama uses /api routes, not /v1. Strip a leaked /v1 suffix."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434/v1")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")
    base = _unwrap(model)
    assert isinstance(base, ChatOllama)
    assert base.base_url == "http://p40.test:11434"
    assert "/v1" not in base.base_url


def test_factory_attaches_metrics_callback_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory binds the LangGraphMetricsCallback + metadata so per-call
    Prometheus labels (agent/group/model) populate without per-call-site config.
    Regression guard against accidental un-wiring."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")
    cfg = _config(model)
    assert "callbacks" in cfg and any(
        isinstance(cb, LangGraphMetricsCallback) for cb in cfg["callbacks"]
    )
    assert cfg["metadata"]["agent"] == "triager"
    assert cfg["metadata"]["group"] == "local-p40"
    assert cfg["metadata"]["model"] == "qwen2.5:7b"


def test_factory_chain_still_supports_with_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory wraps in a RunnableBinding; verify the existing pattern
    `llm(_AGENT_ID).with_structured_output(SomePydantic)` still chains.

    Verified empirically (langchain-core 0.3+ RunnableBinding proxies model
    attributes including with_structured_output via __getattr__). This test
    locks the invariant.
    """

    class _Foo(BaseModel):
        name: str

    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")
    # No crash = invariant holds
    chained = model.with_structured_output(_Foo)
    assert chained is not None
