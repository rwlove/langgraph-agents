"""Smoke tests for the Prometheus + structlog plumbing.

Asserts the metrics are registered with the documented label schema, the
LangChain callback handler emits all four metrics on a synthetic run, and
the FastAPI /metrics endpoint serves the Prometheus exposition format.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.outputs import Generation, LLMResult

from agents.observability import (
    CONTENT_TYPE_LATEST,
    LangGraphMetricsCallback,
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


@pytest.fixture()
def app_client() -> TestClient:
    from agents.main import app  # noqa: PLC0415 — local to avoid import-at-collect

    return TestClient(app)


def test_metrics_endpoint_served_by_fastapi(app_client: TestClient) -> None:
    resp = app_client.get("/metrics")
    assert resp.status_code == 200
    assert "langgraph_calls_total" in resp.text
    assert resp.headers["content-type"].startswith("text/plain")
