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
from agents.router import RouteDecision, estimate_input_tokens, score_route
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
    on_destructive: bool = False,
    on_cascade: bool = False,
    cascade_threshold: int = 2,
) -> Settings:
    return Settings(
        router_scorer_enabled=enabled,
        router_escalate_token_threshold=threshold,
        router_escalate_on_destructive=on_destructive,
        router_escalate_on_cascade=on_cascade,
        router_cascade_threshold=cascade_threshold,
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
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD", "1000")
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
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD", "1000")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=5000)
    with patch("agents.llm.service_healthy", return_value=True):
        model = llm("coder")
    assert isinstance(model, ChatOllama)


def test_llm_emits_router_decision_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_P40_URL", "http://p40.test:11434")
    monkeypatch.setenv("ROUTER_ESCALATE_TOKEN_THRESHOLD", "1000")
    get_settings.cache_clear()
    structlog.contextvars.bind_contextvars(data_tier="internal", est_input_tokens=10)
    child = langgraph_router_decision_total.labels(
        agent="triager", decision="local", reason="local_default"
    )
    before = child._value.get()
    with patch("agents.llm.service_healthy", return_value=True):
        llm("triager")  # triager is local-p40, under threshold -> local_default
    assert child._value.get() == before + 1
