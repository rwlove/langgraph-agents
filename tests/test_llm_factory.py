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
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from agents.health import reset_cache
from agents.llm import AGENT_GROUP, GROUP_MODELS, LocalOllamaUnavailable, llm
from agents.observability import LangGraphMetricsCallback
from agents.settings import get_settings
from agents.state import AgentId


def _metrics_handler(model: Any) -> LangGraphMetricsCallback | None:
    """Find the LangGraphMetricsCallback attached to the model's intrinsic callbacks."""
    callbacks = getattr(model, "callbacks", None) or []
    for cb in callbacks:
        if isinstance(cb, LangGraphMetricsCallback):
            return cb
    return None


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
        f"AGENT_GROUP missing {expected - set(AGENT_GROUP)}, extra {set(AGENT_GROUP) - expected}"
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
        model = llm("historian")  # reporter is local-spark
    assert isinstance(model, ChatOllama)
    assert "spark.test" in model.base_url
    assert model.model == "qwen2.5:32b"
    handler = _metrics_handler(model)
    assert handler is not None
    assert handler.agent == "historian"
    assert handler.group == "local-spark"
    assert handler.model == "qwen2.5:32b"


def test_local_spark_coder_returns_chat_ollama_when_spark_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """coder/reviewer route to the dedicated coder model on Spark.

    Same Ollama instance as local-spark — only the model name differs.
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder")  # coder is local-spark-coder
    assert isinstance(model, ChatOllama)
    assert "spark.test" in model.base_url
    assert model.model == "qwen2.5-coder:32b"
    handler = _metrics_handler(model)
    assert handler is not None
    assert handler.agent == "coder"
    assert handler.group == "local-spark-coder"
    assert handler.model == "qwen2.5-coder:32b"


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
    handler = _metrics_handler(model)
    assert handler is not None
    assert handler.agent == "triager"
    assert handler.group == "local-p40"
    assert handler.model == "qwen2.5:7b"


def test_ollama_num_ctx_is_per_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each group carries its own VRAM-safe num_ctx so prompts aren't truncated.

    The P40 (24GB, shared by five GPU pods) gets the smaller 16384 ceiling; the
    Spark (128GB unified) holds the model's full 32768.
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        p40_model = llm("triager")  # local-p40
        spark_model = llm("historian")  # local-spark
    assert isinstance(p40_model, ChatOllama)
    assert isinstance(spark_model, ChatOllama)
    assert p40_model.num_ctx == 16384
    assert spark_model.num_ctx == 32768


def test_ollama_num_ctx_is_configurable_per_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("OLLAMA_NUM_CTX_P40", "8192")
    monkeypatch.setenv("OLLAMA_NUM_CTX_SPARK", "20000")
    with patch("agents.llm.service_healthy", return_value=True):
        p40_model = llm("triager")  # local-p40
        spark_model = llm("historian")  # local-spark
    assert isinstance(p40_model, ChatOllama)
    assert isinstance(spark_model, ChatOllama)
    assert p40_model.num_ctx == 8192
    assert spark_model.num_ctx == 20000


def test_spark_degraded_to_p40_uses_p40_num_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Spark request that degrades to the P40 must adopt the P40's num_ctx.

    The KV cache is sized for the GPU actually serving — degraded routing must
    not load a 32k cache onto the VRAM-constrained P40.
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")

    def _health(url: str, *, ttl_seconds: float = 60.0) -> bool:
        return "p40.test" in url  # P40 up, Spark down

    with patch("agents.llm.service_healthy", side_effect=_health):
        model = llm("historian")  # local-spark, degrades to P40
    assert isinstance(model, ChatOllama)
    assert "p40.test" in model.base_url
    assert model.num_ctx == 16384  # P40 ceiling, not the Spark's 32768


def test_local_spark_degrades_to_p40_when_spark_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spark down → heavy-agent call routes to P40 (degraded quality, not down).

    Critically, the metric metadata reflects the EFFECTIVE group (local-p40)
    so Grafana shows the actual routing, not the requested group. Coder
    requests degrade to the same P40 general model — qwen2.5:7b's coding
    ability is weak, but the request doesn't fail.
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")

    def _health(url: str, *, ttl_seconds: float = 60.0) -> bool:
        return "p40.test" in url  # P40 up, Spark down

    with patch("agents.llm.service_healthy", side_effect=_health):
        model = llm("coder")  # local-spark-coder, degrades to P40
    assert isinstance(model, ChatOllama)
    assert "p40.test" in model.base_url
    assert model.model == "qwen2.5:7b"  # the degraded model, NOT the original coder 32b
    handler = _metrics_handler(model)
    assert handler is not None
    # Effective group is local-p40 (the one actually serving) so metric labels
    # tell the truth about the degraded routing.
    assert handler.group == "local-p40"
    assert handler.model == "qwen2.5:7b"
    assert handler.agent == "coder"


def test_local_p40_has_no_blackwell_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """P40 down → light-agent call raises (not degrade up to Spark — that'd waste GPU)."""
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(LocalOllamaUnavailable) as excinfo,
    ):
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

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(LocalOllamaUnavailable) as excinfo,
    ):
        llm("historian")  # reporter is local-spark
    assert excinfo.value.group == "local-spark"
    assert excinfo.value.failed_group == "local-spark"


def test_local_spark_coder_raises_with_failed_group_local_spark_coder_when_both_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both local paths down for a coder request → raise with the originally-requested group.

    The /inbox queue routes retries by `failed_group`, so reporting the
    distinct group preserves coder-specific recovery semantics (don't retry
    on Spark coming back if the general model is what's healthy, etc.).
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(LocalOllamaUnavailable) as excinfo,
    ):
        llm("coder")  # coder is local-spark-coder
    assert excinfo.value.group == "local-spark-coder"
    assert excinfo.value.failed_group == "local-spark-coder"


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


def test_master_switch_off_blocks_escalation_degrades_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENABLE_CLAUDE_API=false refuses escalation even with a key set + escalate=True;
    the request degrades to the local group instead of touching Claude."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_CLAUDE_API", "false")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder", escalate=True)  # coder is local-spark-coder
    assert isinstance(model, ChatOllama)


def test_master_switch_off_raises_on_explicit_claude_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit group_override='claude' cannot be satisfied with the master
    switch off — it raises rather than silently degrading."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_CLAUDE_API", "false")
    with patch("agents.llm.service_healthy", return_value=True):
        with pytest.raises(RuntimeError, match="ENABLE_CLAUDE_API is false"):
            llm("coder", group_override="claude")


def test_master_switch_off_blocks_degraded_mode_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with degraded-mode escalation enabled and both local paths down,
    the master switch off forces LocalOllamaUnavailable instead of Claude."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_CLAUDE_API", "false")
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("DEGRADED_MODE_ESCALATION_ENABLED", "true")
    with patch("agents.llm.service_healthy", return_value=False):
        with pytest.raises(LocalOllamaUnavailable):
            llm("coder")


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


def test_factory_attaches_metrics_callback_with_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory attaches LangGraphMetricsCallback as an intrinsic model
    callback with agent/group/model labels baked into the handler instance.

    Why intrinsic and not `with_config(callbacks=[...])`: empirically verified
    against langchain-core 0.3+ that `with_config(callbacks=[...])` does NOT
    survive `with_structured_output()` chain wrapping (the resulting
    RunnableSequence drops the callback). Intrinsic model callbacks survive.
    Regression guard.
    """
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")
    handler = _metrics_handler(model)
    assert handler is not None
    assert handler.agent == "triager"
    assert handler.group == "local-p40"
    assert handler.model == "qwen2.5:7b"


def test_factory_chain_still_supports_with_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory returns a bare ChatOllama (not a RunnableBinding) so the
    existing per-node pattern `llm(_AGENT_ID).with_structured_output(X)`
    chains normally. Locks the invariant against future refactors that might
    re-introduce a binding wrapper.
    """

    class _Foo(BaseModel):
        name: str

    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("triager")
    chained = model.with_structured_output(_Foo)
    assert chained is not None
