"""Tests for `agents.router` — the deterministic local-vs-Claude scorer.

Two layers:
- Pure `score_route` matrix — one case per RouteDecision.reason branch plus
  the boundary and unbound-estimate edges. No app/queue/TestClient: signals
  are bound directly into structlog contextvars.
- `llm()` integration — the scorer flips a local agent to the Claude path on
  context overflow *only* when ANTHROPIC_API_KEY is set (graceful-degrade
  otherwise), and the decision metric increments on every call.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama

from agents.health import reset_cache
from agents.llm import llm
from agents.observability import langgraph_router_decision_total
from agents.router import (
    RouteDecision,
    estimate_input_tokens,
    is_claude_code_source,
    score_route,
)
from agents.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_contextvars() -> None:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    reset_cache()


def _settings(
    *,
    enabled: bool = True,
    threshold: int = 1000,
    threshold_p40: int | None = None,
    on_destructive: bool = False,
    on_cascade: bool = False,
    cascade_threshold: int = 2,
    suppress_claude_code: bool = True,
) -> Settings:
    # `threshold` sets BOTH per-group bars by default so the single-threshold
    # matrix below is group-agnostic; `threshold_p40` overrides just the P40 bar
    # for the per-group differentiation cases.
    return Settings(
        router_scorer_enabled=enabled,
        router_escalate_token_threshold_p40=(
            threshold_p40 if threshold_p40 is not None else threshold
        ),
        router_escalate_token_threshold_spark=threshold,
        router_escalate_on_destructive=on_destructive,
        router_escalate_on_cascade=on_cascade,
        router_cascade_threshold=cascade_threshold,
        router_suppress_escalation_for_claude_code=suppress_claude_code,
    )


# --- estimate helper ---------------------------------------------------------


def test_estimate_input_tokens_applies_overhead_and_is_monotonic() -> None:
    assert estimate_input_tokens("") == 2000  # pure overhead, no content
    small = estimate_input_tokens("x" * 400)  # 100 content + 2000 overhead
    large = estimate_input_tokens("x" * 4000)  # 1000 content + 2000 overhead
    assert small == 2100
    assert large == 3000
    assert large > small


# --- pure score_route matrix -------------------------------------------------


def test_escalates_on_context_overflow() -> None:
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=5000)
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=True, reason="context_overflow")


def test_local_default_under_threshold() -> None:
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=500)
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="local_default")


def test_restricted_tier_never_escalates_even_over_threshold() -> None:
    structlog.contextvars.bind_contextvars(data_tier="restricted", est_input_tokens=99999)
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="restricted_pinned_local")


def test_kill_switch_disables_scorer() -> None:
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=99999)
    decision = score_route("coder", "local-spark-coder", _settings(enabled=False, threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="scorer_disabled")


def test_unbound_estimate_stays_local() -> None:
    # No est_input_tokens contextvar (directly-enqueued / legacy task) -> 0.
    structlog.contextvars.bind_contextvars(data_tier="internal")
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="local_default")


def test_boundary_exactly_at_threshold_stays_local() -> None:
    # Strict > — equal to the threshold does not escalate.
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=1000)
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision.escalate is False
    assert decision.reason == "local_default"


def test_p40_uses_its_own_lower_threshold() -> None:
    # Same prompt size, two groups: over the P40 bar but under the Spark bar.
    # local-p40 escalates (qwen2.5:7b's KV cache can't hold it); local-spark
    # stays local (the 32b's larger cache fits it).
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=1500)
    s = _settings(threshold=2000, threshold_p40=1000)
    assert score_route("triager", "local-p40", s) == RouteDecision(
        escalate=True, reason="context_overflow"
    )
    assert score_route("historian", "local-spark", s) == RouteDecision(
        escalate=False, reason="local_default"
    )


def test_spark_coder_uses_spark_threshold() -> None:
    # local-spark-coder shares the Spark ceiling, not the P40 one.
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=1500)
    s = _settings(threshold=2000, threshold_p40=1000)
    assert score_route("coder", "local-spark-coder", s) == RouteDecision(
        escalate=False, reason="local_default"
    )


def test_claude_group_is_a_noop() -> None:
    # Scorer only ever flips local -> claude; an already-claude call is left alone.
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=99999)
    decision = score_route("coder", "claude", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="local_default")


# --- opt-in triggers (default OFF) -------------------------------------------


def test_destructive_does_not_escalate_by_default() -> None:
    # Flag defaults False — a destructive task stays local under threshold.
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=10, destructive=True
    )
    decision = score_route("coder", "local-spark-coder", _settings())
    assert decision == RouteDecision(escalate=False, reason="local_default")


def test_destructive_escalates_when_enabled() -> None:
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=10, destructive=True
    )
    decision = score_route("coder", "local-spark-coder", _settings(on_destructive=True))
    assert decision == RouteDecision(escalate=True, reason="destructive_escalation")


def test_destructive_false_stays_local_when_enabled() -> None:
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=10, destructive=False
    )
    decision = score_route("coder", "local-spark-coder", _settings(on_destructive=True))
    assert decision == RouteDecision(escalate=False, reason="local_default")


def test_destructive_restricted_still_pinned_local_when_enabled() -> None:
    # Restricted-tier guard precedes the opt-in triggers — never escalates.
    structlog.contextvars.bind_contextvars(
        data_tier="restricted", est_input_tokens=10, destructive=True
    )
    decision = score_route("coder", "local-spark-coder", _settings(on_destructive=True))
    assert decision == RouteDecision(escalate=False, reason="restricted_pinned_local")


def test_cascade_does_not_escalate_by_default() -> None:
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=10, cascade_count=5
    )
    decision = score_route("coder", "local-spark-coder", _settings())
    assert decision == RouteDecision(escalate=False, reason="local_default")


def test_cascade_escalates_at_threshold_when_enabled() -> None:
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=10, cascade_count=2
    )
    decision = score_route(
        "coder", "local-spark-coder", _settings(on_cascade=True, cascade_threshold=2)
    )
    assert decision == RouteDecision(escalate=True, reason="cascade_escalation")


def test_cascade_below_threshold_stays_local_when_enabled() -> None:
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=10, cascade_count=1
    )
    decision = score_route(
        "coder", "local-spark-coder", _settings(on_cascade=True, cascade_threshold=2)
    )
    assert decision == RouteDecision(escalate=False, reason="local_default")


def test_context_overflow_wins_over_opt_in_triggers() -> None:
    # Stronger capability reason reported when multiple conditions hold.
    structlog.contextvars.bind_contextvars(
        data_tier="internal", est_input_tokens=99999, destructive=True, cascade_count=9
    )
    decision = score_route(
        "coder",
        "local-spark-coder",
        _settings(on_destructive=True, on_cascade=True),
    )
    assert decision == RouteDecision(escalate=True, reason="context_overflow")


# --- llm() integration -------------------------------------------------------


def test_llm_escalates_to_claude_on_overflow_when_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD_SPARK", "1000")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=5000)
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder")  # coder is local-spark-coder
    assert isinstance(model, ChatAnthropic)


def test_llm_stays_local_on_overflow_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Graceful degrade: scorer decides escalate, but no key -> falls through
    # to the local group exactly as today. No new failure path.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD_SPARK", "1000")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=5000)
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder")
    assert isinstance(model, ChatOllama)


def test_llm_emits_router_decision_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD_P40", "1000")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=10)
    child = langgraph_router_decision_total.labels(
        agent="triager", decision="local", reason="local_default"
    )
    before = child._value.get()
    with patch("agents.llm.service_healthy", return_value=True):
        llm("triager")  # triager is local-p40, under threshold -> local_default
    assert child._value.get() == before + 1


# --- Path-2: claude-code-origin no-escalate guard ----------------------------


def test_claude_code_origin_pinned_local_even_over_threshold() -> None:
    structlog.contextvars.bind_contextvars(
        data_tier="internal", source="claude-code", est_input_tokens=99999
    )
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="claude_code_origin")


def test_claude_code_suppression_can_be_disabled() -> None:
    # Guard off -> a Claude-Code task escalates on overflow like any other.
    structlog.contextvars.bind_contextvars(
        data_tier="internal", source="claude-code", est_input_tokens=99999
    )
    decision = score_route(
        "coder", "local-spark-coder", _settings(threshold=1000, suppress_claude_code=False)
    )
    assert decision == RouteDecision(escalate=True, reason="context_overflow")


def test_restricted_precedes_claude_code() -> None:
    # Both pin local; restricted is the stronger invariant and reported first.
    structlog.contextvars.bind_contextvars(
        data_tier="restricted", source="claude-code", est_input_tokens=99999
    )
    decision = score_route("coder", "local-spark-coder", _settings(threshold=1000))
    assert decision == RouteDecision(escalate=False, reason="restricted_pinned_local")


def test_is_claude_code_source_helper() -> None:
    structlog.contextvars.bind_contextvars(source="claude-code")
    assert is_claude_code_source(_settings()) is True
    assert is_claude_code_source(_settings(suppress_claude_code=False)) is False


def test_is_claude_code_source_false_for_other_sources() -> None:
    structlog.contextvars.bind_contextvars(source="zulip")
    assert is_claude_code_source(_settings()) is False


def test_llm_claude_code_origin_stays_local_on_overflow_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scorer would escalate on overflow, but the Path-2 guard pins it local even
    # with a key set — Claude-Code offloads never reach the metered API.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD_SPARK", "1000")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(
        data_tier="internal", source="claude-code", est_input_tokens=5000
    )
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder")
    assert isinstance(model, ChatOllama)


def test_llm_claude_code_origin_suppresses_explicit_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The explicit escalate=True path the scorer never sees is also suppressed.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("OLLAMA_SPARK_URL", "http://spark.test:11434")
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(
        data_tier="internal", source="claude-code", est_input_tokens=10
    )
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder", escalate=True)
    assert isinstance(model, ChatOllama)
