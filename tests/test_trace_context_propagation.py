"""Tests for OTel trace-context propagation across the queue boundary.

The worker drains the queue in a *separate* process from /inbox, so the
live FastAPI server span isn't in scope when the worker runs. Before this
fix the worker's `queue.process` span opened a brand-new trace and every
span for the task was orphaned from the ingress root (home-ops DoD
2026-05-31). The fix serializes the W3C trace context into
``envelope["otel_carrier"]`` at ingress (`inbox._inject_trace_context`)
and rebuilds it in the worker (`worker._extract_trace_context`) so the
worker span continues the same trace.

These tests pin: injection writes a carrier under a live span and is a
no-op without one; extraction is a guarded None for missing/malformed
carriers; and the full round-trip yields a worker span whose trace_id
matches the ingress span (i.e. the orphan is gone).
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agents.api.inbox import _inject_trace_context
from agents.queue.worker import _extract_trace_context


@pytest.fixture
def tracer_provider() -> TracerProvider:
    """A real SDK provider so span contexts are valid (the no-op API
    provider yields INVALID contexts that propagators refuse to inject).
    Set as the global provider for the duration of the test; OTel forbids
    re-setting, so we swap the private slot back on teardown.
    """
    provider = TracerProvider()
    previous = trace.get_tracer_provider()
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    yield provider
    trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]


def test_inject_writes_carrier_under_active_span(tracer_provider: TracerProvider) -> None:
    tracer = tracer_provider.get_tracer("test")
    envelope: dict = {}
    with tracer.start_as_current_span("ingress"):
        _inject_trace_context(envelope)
    assert "otel_carrier" in envelope
    assert "traceparent" in envelope["otel_carrier"]


def test_inject_is_noop_without_active_span() -> None:
    # No SDK provider / no live span → invalid context → nothing to inject.
    envelope: dict = {}
    _inject_trace_context(envelope)
    assert "otel_carrier" not in envelope


def test_extract_returns_none_for_missing_carrier() -> None:
    assert _extract_trace_context({"content": "hi"}) is None


def test_extract_returns_none_for_malformed_carrier() -> None:
    # Carrier present but not a dict → guarded None, never raises.
    assert _extract_trace_context({"otel_carrier": "not-a-dict"}) is None


def test_roundtrip_worker_span_continues_ingress_trace(
    tracer_provider: TracerProvider,
) -> None:
    """The fix's core guarantee: the worker span shares the ingress trace."""
    tracer = tracer_provider.get_tracer("test")
    envelope: dict = {}

    # Ingress: capture the live trace id, inject the carrier into the envelope.
    with tracer.start_as_current_span("ingress") as ingress_span:
        ingress_trace_id = ingress_span.get_span_context().trace_id
        _inject_trace_context(envelope)

    # Worker (separate scope, no live parent): extract + start child span.
    parent_ctx = _extract_trace_context(envelope)
    assert parent_ctx is not None
    with tracer.start_as_current_span("queue.process", context=parent_ctx) as worker_span:
        worker_ctx = worker_span.get_span_context()

    assert worker_ctx.trace_id == ingress_trace_id


def test_roundtrip_falls_back_to_root_without_carrier(
    tracer_provider: TracerProvider,
) -> None:
    """A directly-enqueued task (no carrier) gets a clean root span, not a crash."""
    tracer = tracer_provider.get_tracer("test")
    parent_ctx = _extract_trace_context({"content": "directly enqueued"})
    assert parent_ctx is None
    with tracer.start_as_current_span("queue.process", context=parent_ctx) as span:
        # Root span: valid context, and no remote parent was grafted on.
        assert span.get_span_context().is_valid
