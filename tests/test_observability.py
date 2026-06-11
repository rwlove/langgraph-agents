"""Smoke tests for the Prometheus + structlog plumbing.

Asserts the metrics are registered with the documented label schema, the
LangChain callback handler emits all four metrics on a synthetic run, and
the FastAPI /metrics endpoint serves the Prometheus exposition format.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog
from fastapi.testclient import TestClient
from langchain_core.outputs import Generation, LLMResult

from agents.observability import (
    CONTENT_TYPE_LATEST,
    LangGraphMetricsCallback,
    _compute_anthropic_cost,
    configure_structlog,
    get_logger,
    langgraph_calls_total,
    langgraph_cost_usd_total,
    langgraph_llm_duration_seconds,
    langgraph_tokens_total,
    metrics_text,
    record_llm_call,
)


def _label_names(counter) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    return counter._labelnames


def test_metric_label_schemas_match_v20_plan() -> None:
    """The v20 plan locked these label sets; phase 2/4/6 + Grafana depend on them."""
    assert _label_names(langgraph_calls_total) == (
        "agent",
        "group",
        "model",
        "outcome",
        "trigger",
    )
    assert _label_names(langgraph_tokens_total) == (
        "agent",
        "group",
        "model",
        "direction",
    )
    assert _label_names(langgraph_cost_usd_total) == ("agent", "group", "model")
    assert _label_names(langgraph_llm_duration_seconds) == ("agent", "group", "model")


def test_record_llm_call_emits_all_four_metrics() -> None:
    before_calls = langgraph_calls_total.labels(
        agent="t1", group="local", model="x", outcome="success", trigger=""
    )._value.get()
    before_in = langgraph_tokens_total.labels(
        agent="t1", group="local", model="x", direction="in"
    )._value.get()
    before_out = langgraph_tokens_total.labels(
        agent="t1", group="local", model="x", direction="out"
    )._value.get()

    record_llm_call(
        agent="t1",
        group="local",
        model="x",
        tokens_in=100,
        tokens_out=42,
        cost_usd=0.001,
        duration_seconds=0.123,
    )

    after_calls = langgraph_calls_total.labels(
        agent="t1", group="local", model="x", outcome="success", trigger=""
    )._value.get()
    after_in = langgraph_tokens_total.labels(
        agent="t1", group="local", model="x", direction="in"
    )._value.get()
    after_out = langgraph_tokens_total.labels(
        agent="t1", group="local", model="x", direction="out"
    )._value.get()

    assert after_calls == before_calls + 1
    assert after_in == before_in + 100
    assert after_out == before_out + 42


def test_callback_emits_on_llm_end() -> None:
    cb = LangGraphMetricsCallback(agent="cb-test", group="local", model="y")
    run_id = uuid4()

    cb.on_llm_start(serialized={}, prompts=["hello"], run_id=run_id)
    fake_result = LLMResult(
        generations=[[Generation(text="hi")]],
        llm_output={"token_usage": {"prompt_tokens": 5, "completion_tokens": 1}},
    )
    cb.on_llm_end(fake_result, run_id=run_id)

    calls = langgraph_calls_total.labels(
        agent="cb-test", group="local", model="y", outcome="success", trigger=""
    )._value.get()
    tokens_in = langgraph_tokens_total.labels(
        agent="cb-test", group="local", model="y", direction="in"
    )._value.get()
    assert calls >= 1
    assert tokens_in >= 5


def test_callback_on_llm_error_records_error_outcome() -> None:
    cb = LangGraphMetricsCallback(agent="cb-err", group="local", model="z")
    run_id = uuid4()

    cb.on_llm_start(serialized={}, prompts=["hello"], run_id=run_id)
    cb.on_llm_error(RuntimeError("boom"), run_id=run_id)

    errors = langgraph_calls_total.labels(
        agent="cb-err", group="local", model="z", outcome="error", trigger=""
    )._value.get()
    assert errors >= 1


def test_trigger_label_used_for_claude_leg() -> None:
    """Phase 6 wiring: when group=claude, trigger should propagate to the counter."""
    record_llm_call(
        agent="coder",
        group="claude",
        model="claude-opus-4-7",
        trigger="requires_cloud",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.05,
    )
    val = langgraph_calls_total.labels(
        agent="coder",
        group="claude",
        model="claude-opus-4-7",
        outcome="success",
        trigger="requires_cloud",
    )._value.get()
    assert val >= 1


def test_metrics_text_returns_prometheus_format() -> None:
    payload, content_type = metrics_text()
    assert content_type == CONTENT_TYPE_LATEST
    assert b"langgraph_calls_total" in payload
    assert b"# TYPE langgraph_calls_total counter" in payload


def test_get_logger_binds_component_only_not_agent() -> None:
    """Regression: ``get_logger`` must not bind ``agent`` at construction time.

    A logger-level ``agent`` binding would outrank the per-node
    ``structlog.contextvars.bind_contextvars(agent=...)`` set by
    ``graphs/fleet.py:_with_activity_log`` and silently mislabel every
    structlog event a node emits through a shared helper (api surfaces,
    graph wrapper, paused-threads sweeper). See v0.2.24 changelog.

    Verifies the real processor pipeline (including
    ``merge_contextvars``) — ``structlog.testing.capture_logs`` bypasses
    processors, so we drive the full chain through a ``LogCapture``
    processor and a structlog config that mirrors production.
    """
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.BoundLogger,
        cache_logger_on_first_use=False,
    )
    try:
        slog = get_logger("graphs.fleet")
        structlog.contextvars.clear_contextvars()
        token = structlog.contextvars.bind_contextvars(agent="triager")
        try:
            slog.info("node_start", task_id="lf-test")
        finally:
            structlog.contextvars.reset_contextvars(**token)
            structlog.contextvars.clear_contextvars()
    finally:
        # Restore production structlog config for other tests.
        configure_structlog()

    assert len(cap.entries) == 1
    event = cap.entries[0]
    # The contextvar's `agent=triager` must win, not be shadowed by a
    # logger-level binding.
    assert event.get("agent") == "triager", event
    # And the component label rides along on the same event.
    assert event.get("component") == "graphs.fleet", event
    assert event["event"] == "node_start"
    assert event["task_id"] == "lf-test"


def test_get_logger_default_component_is_agents() -> None:
    """``get_logger()`` with no arg falls back to ``component=agents``
    and does not bind any ``agent`` field."""
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.BoundLogger,
        cache_logger_on_first_use=False,
    )
    try:
        slog = get_logger()
        structlog.contextvars.clear_contextvars()
        slog.info("ping")
    finally:
        configure_structlog()
    assert cap.entries[0].get("component") == "agents"
    # No agent label when neither caller nor contextvar provides one.
    assert "agent" not in cap.entries[0]


@pytest.fixture()
def app_client() -> TestClient:
    from agents.main import app  # noqa: PLC0415 — local to avoid import-at-collect

    return TestClient(app)


def test_metrics_endpoint_served_by_fastapi(app_client: TestClient) -> None:
    resp = app_client.get("/metrics")
    assert resp.status_code == 200
    assert "langgraph_calls_total" in resp.text
    assert resp.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# Anthropic pricing table tests
# ---------------------------------------------------------------------------


def test_compute_anthropic_cost_opus() -> None:
    """1M input + 1M output at Opus pricing = $15 + $75 = $90."""
    cost = _compute_anthropic_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    assert abs(cost - 90.0) < 0.01, cost


def test_compute_anthropic_cost_sonnet() -> None:
    """1M input + 1M output at Sonnet 4 pricing = $3 + $15 = $18."""
    cost = _compute_anthropic_cost("claude-sonnet-4-5", 1_000_000, 1_000_000)
    assert abs(cost - 18.0) < 0.01, cost


def test_compute_anthropic_cost_haiku() -> None:
    """1M input + 1M output at Haiku 3.5 pricing = $0.8 + $4 = $4.8."""
    cost = _compute_anthropic_cost("claude-haiku-3-5", 1_000_000, 1_000_000)
    assert abs(cost - 4.8) < 0.01, cost


def test_compute_anthropic_cost_unknown_model_returns_zero() -> None:
    """Local Ollama models and unrecognised strings return 0.0."""
    assert _compute_anthropic_cost("qwen3-next:80b-a3b-instruct-q4_K_M", 100_000, 50_000) == 0.0
    assert _compute_anthropic_cost("", 100_000, 50_000) == 0.0
    assert _compute_anthropic_cost("claude-future-9", 100_000, 50_000) == 0.0


def test_compute_anthropic_cost_small_call() -> None:
    """100 input tokens + 50 output tokens at Sonnet 4 pricing."""
    # $3/MTok input: 100 tokens = $0.0003
    # $15/MTok output: 50 tokens = $0.00075
    # total = $0.00105
    cost = _compute_anthropic_cost("claude-sonnet-4-5", 100, 50)
    assert abs(cost - 0.00105) < 1e-9, cost


def test_callback_emits_cost_usd_for_claude_model() -> None:
    """LangGraphMetricsCallback.on_llm_end populates cost for Claude models."""
    model = "claude-sonnet-4-5"
    cb = LangGraphMetricsCallback(agent="cost-test", group="claude", model=model)
    run_id = uuid4()

    tokens_in, tokens_out = 1_000_000, 1_000_000  # exactly $18 at Sonnet 4 pricing
    cb.on_llm_start(serialized={}, prompts=["test"], run_id=run_id)
    fake_result = LLMResult(
        generations=[[Generation(text="result")]],
        llm_output={"token_usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}},
    )
    cb.on_llm_end(fake_result, run_id=run_id)

    cost_val = langgraph_cost_usd_total.labels(
        agent="cost-test", group="claude", model=model
    )._value.get()
    assert abs(cost_val - 18.0) < 0.01, f"expected ~$18 cost, got {cost_val}"


def test_callback_emits_zero_cost_for_local_model() -> None:
    """LangGraphMetricsCallback.on_llm_end emits zero cost for Ollama models."""
    model = "qwen3-next:80b-a3b-instruct-q4_K_M"
    cb = LangGraphMetricsCallback(agent="local-cost-test", group="local-spark", model=model)
    run_id = uuid4()

    before = langgraph_cost_usd_total.labels(
        agent="local-cost-test", group="local-spark", model=model
    )._value.get()

    cb.on_llm_start(serialized={}, prompts=["test"], run_id=run_id)
    fake_result = LLMResult(
        generations=[[Generation(text="result")]],
        llm_output={"token_usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    )
    cb.on_llm_end(fake_result, run_id=run_id)

    after = langgraph_cost_usd_total.labels(
        agent="local-cost-test", group="local-spark", model=model
    )._value.get()
    assert after == before, f"local model should emit zero cost, got delta {after - before}"
