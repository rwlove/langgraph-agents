"""Prometheus metrics + structured-log emission for LLM calls.

Path A (per plan v5): metrics → Prometheus via `/metrics`; structured logs
→ stdout JSON, picked up by the cluster's Vector → Loki pipeline. No OTLP /
no Tempo until per-task tracing becomes a real debugging gap.

This module exposes:

- Four metrics that consumers update on each LLM call:
  - ``langgraph_calls_total{agent,group,model,outcome,trigger}``
  - ``langgraph_tokens_total{agent,group,model,direction}``
  - ``langgraph_cost_usd_total{agent,group,model}``
  - ``langgraph_llm_duration_seconds{agent,group,model}`` (histogram)
- A ``LangGraphMetricsCallback`` LangChain callback handler that emits the
  above on each LLM run, given the agent/group/model context.
- A ``record_llm_call(...)`` helper for non-LangChain emission paths
  (direct ``ollama.Client`` use, batch jobs, etc.).
- A ``get_logger(agent_id)`` factory returning a `structlog` BoundLogger
  pre-bound with ``agent`` and ``component`` fields.

The label set is the v20-locked schema and shared across phases 2/4/6.
Phase 2's LLM factory will be the primary emitter; phase 6's Claude leg
adds ``trigger=requires_cloud|degraded_mode|policy_allowlist`` to the
``calls_total`` counter via the same handler.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult

# ---------------------------------------------------------------------------
# Metric definitions (label set is the v20 schema; do not add labels without
# updating the plan + downstream Grafana queries).
# ---------------------------------------------------------------------------

_LABELS_CALL = ("agent", "group", "model", "outcome", "trigger")
_LABELS_TOK = ("agent", "group", "model", "direction")
_LABELS_COST = ("agent", "group", "model")

langgraph_calls_total = Counter(
    "langgraph_calls_total",
    "LLM invocations from a langgraph-agents node.",
    _LABELS_CALL,
)

langgraph_tokens_total = Counter(
    "langgraph_tokens_total",
    "Tokens transferred per LLM invocation, in or out.",
    _LABELS_TOK,
)

langgraph_cost_usd_total = Counter(
    "langgraph_cost_usd_total",
    "Cumulative LLM cost in USD; zero for local-group calls, non-zero for "
    "Claude-group calls priced from the Anthropic per-token table.",
    _LABELS_COST,
)

langgraph_llm_duration_seconds = Histogram(
    "langgraph_llm_duration_seconds",
    "Wall-clock duration of LLM invocations.",
    _LABELS_COST,
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)


# ---------------------------------------------------------------------------
# Structlog setup. Idempotent; calling configure_structlog() twice is safe.
# ---------------------------------------------------------------------------


def configure_structlog(level: str = "INFO") -> None:
    """Configure structlog to emit one JSON line per event to stdout.

    Vector (deployed at kubernetes/apps/observability/vector/ in the cluster)
    auto-scrapes Pod stdout and ships to Loki. JSON output gives LogQL the
    structured fields it needs to filter on agent/task_id/model/etc.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(agent_id: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger pre-bound with `component=agents` and
    an optional `agent` field. Callers should `.bind(task_id=...)` per request.
    """
    log = structlog.get_logger("agents")
    bindings: dict[str, Any] = {"component": "agents"}
    if agent_id is not None:
        bindings["agent"] = agent_id
    return cast("structlog.stdlib.BoundLogger", log.bind(**bindings))


# ---------------------------------------------------------------------------
# Emission helpers
# ---------------------------------------------------------------------------


def record_llm_call(
    *,
    agent: str,
    group: str,
    model: str,
    outcome: str = "success",
    trigger: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
) -> None:
    """Emit all four metrics for one LLM invocation.

    Direct emission path for code that isn't using a LangChain callback
    (e.g., a raw ``ollama.Client`` call or a batch script). Phase 2's
    factory will prefer the ``LangGraphMetricsCallback`` handler instead,
    which fires automatically on each LangChain run.
    """
    langgraph_calls_total.labels(
        agent=agent, group=group, model=model, outcome=outcome, trigger=trigger
    ).inc()
    if tokens_in:
        langgraph_tokens_total.labels(
            agent=agent, group=group, model=model, direction="in"
        ).inc(tokens_in)
    if tokens_out:
        langgraph_tokens_total.labels(
            agent=agent, group=group, model=model, direction="out"
        ).inc(tokens_out)
    if cost_usd:
        langgraph_cost_usd_total.labels(agent=agent, group=group, model=model).inc(
            cost_usd
        )
    if duration_seconds:
        langgraph_llm_duration_seconds.labels(
            agent=agent, group=group, model=model
        ).observe(duration_seconds)


@contextmanager
def llm_timer() -> Iterator[dict[str, float]]:
    """Convenience context manager: yields a dict; ``["duration"]`` is set on
    exit. Use with ``record_llm_call(..., duration_seconds=timer["duration"])``.
    """
    out: dict[str, float] = {}
    t0 = time.perf_counter()
    try:
        yield out
    finally:
        out["duration"] = time.perf_counter() - t0


# ---------------------------------------------------------------------------
# LangChain callback handler — primary emission path for Phase 2's factory
# ---------------------------------------------------------------------------


class LangGraphMetricsCallback(BaseCallbackHandler):
    """Emits the four langgraph_* metrics on each chat-model run.

    Required metadata on the run config:

    - ``agent``: str — agent_id (one of ``AgentId``)
    - ``group``: str — model group (``local-spark`` | ``local-p40`` | ``claude``)
    - ``model``: str — concrete model name (e.g. ``qwen2.5:32b``)
    - ``trigger`` (optional): str — for Claude-leg calls only

    Attach via ``.with_config(callbacks=[LangGraphMetricsCallback()])`` in the
    Phase 2 factory; the agent/group/model fields flow via the same config's
    ``metadata`` map.
    """

    raise_error = False

    def __init__(self) -> None:
        super().__init__()
        self._starts: dict[UUID, float] = {}

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **_: Any,
    ) -> None:
        self._starts[run_id] = time.perf_counter()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **_: Any,
    ) -> None:
        # Some chat models call this instead of on_llm_start.
        self._starts[run_id] = time.perf_counter()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        duration = time.perf_counter() - self._starts.pop(run_id, time.perf_counter())
        meta = metadata or {}
        agent = str(meta.get("agent", "unknown"))
        group = str(meta.get("group", "unknown"))
        model = str(meta.get("model", "unknown"))
        trigger = str(meta.get("trigger", ""))

        tokens_in = 0
        tokens_out = 0
        if response.llm_output:
            usage = response.llm_output.get("token_usage") or response.llm_output.get(
                "usage"
            ) or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

        record_llm_call(
            agent=agent,
            group=group,
            model=model,
            outcome="success",
            trigger=trigger,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=duration,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        duration = time.perf_counter() - self._starts.pop(run_id, time.perf_counter())
        meta = metadata or {}
        record_llm_call(
            agent=str(meta.get("agent", "unknown")),
            group=str(meta.get("group", "unknown")),
            model=str(meta.get("model", "unknown")),
            outcome="error",
            trigger=str(meta.get("trigger", "")),
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# ASGI export for FastAPI mount
# ---------------------------------------------------------------------------


def metrics_text() -> tuple[bytes, str]:
    """Return (payload, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


__all__ = [
    "CONTENT_TYPE_LATEST",
    "LangGraphMetricsCallback",
    "configure_structlog",
    "get_logger",
    "langgraph_calls_total",
    "langgraph_cost_usd_total",
    "langgraph_llm_duration_seconds",
    "langgraph_tokens_total",
    "llm_timer",
    "metrics_text",
    "record_llm_call",
]
