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
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler as LangfuseLangchainCallback
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from agents.settings import get_settings

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult


_LANGFUSE_LOGGER = logging.getLogger("agents.observability.langfuse")
_langfuse_client: Langfuse | None = None

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

    Labels (``agent``, ``group``, ``model``, ``trigger``) are supplied at
    construction so they survive any chain wrapping (e.g. ``with_structured
    _output``). The factory creates a fresh handler per llm() call with the
    correct per-agent labels and attaches it as an intrinsic model callback
    (``ChatOllama(callbacks=[handler], ...)``) — that path fires reliably,
    whereas ``with_config(callbacks=[...])`` followed by ``with_structured_
    output()`` silently drops the callback (verified empirically against
    langchain-core 0.3+).

    A previous version of this handler relied on ``metadata`` propagated via
    ``with_config(metadata={...})`` — that approach was abandoned because
    LangChain's internal ``ls_*`` metadata wins the merge and user metadata
    is dropped at the ``on_chat_model_start`` / ``on_llm_end`` boundary.
    """

    raise_error = False

    def __init__(
        self,
        *,
        agent: str,
        group: str,
        model: str,
        trigger: str = "",
    ) -> None:
        super().__init__()
        self.agent = agent
        self.group = group
        self.model = model
        self.trigger = trigger
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
        **_: Any,
    ) -> None:
        duration = time.perf_counter() - self._starts.pop(run_id, time.perf_counter())
        tokens_in = 0
        tokens_out = 0
        if response.llm_output:
            usage = response.llm_output.get("token_usage") or response.llm_output.get(
                "usage"
            ) or {}
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)

        record_llm_call(
            agent=self.agent,
            group=self.group,
            model=self.model,
            outcome="success",
            trigger=self.trigger,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=duration,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **_: Any,
    ) -> None:
        duration = time.perf_counter() - self._starts.pop(run_id, time.perf_counter())
        record_llm_call(
            agent=self.agent,
            group=self.group,
            model=self.model,
            outcome="error",
            trigger=self.trigger,
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# ASGI export for FastAPI mount
# ---------------------------------------------------------------------------


def metrics_text() -> tuple[bytes, str]:
    """Return (payload, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# Langfuse — per-task trace UI (self-hosted; see kubernetes/apps/ai/langfuse/)
# ---------------------------------------------------------------------------


def init_langfuse() -> None:
    """Initialize the process-wide Langfuse client from settings.

    Idempotent and safe to call at app startup. If any of
    LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY is unset
    (the in-cluster langfuse hasn't been provisioned yet, or the
    project keys haven't been copied to 1Password yet), tracing is
    silently disabled and the factory in ``agents.llm`` skips
    attaching the Langfuse callback.

    The client picks up trace context from the LangChain callback's
    ``run_id`` and merges per-call metadata into a langfuse trace. We
    don't need to drive trace lifecycle manually — the LangChain
    integration handles it as long as the CallbackHandler is attached
    to each chat-model instance.
    """
    global _langfuse_client  # noqa: PLW0603
    settings = get_settings()
    if _langfuse_client is not None:
        return
    if not (
        settings.langfuse_host
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    ):
        _LANGFUSE_LOGGER.info(
            "langfuse keys not configured; per-task tracing disabled"
        )
        return

    _langfuse_client = Langfuse(
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )
    _LANGFUSE_LOGGER.info(
        "langfuse client initialized (host=%s)", settings.langfuse_host
    )


def langfuse_callback_handler() -> BaseCallbackHandler | None:
    """Return a fresh LangChain CallbackHandler bound to the process
    Langfuse client, or None when tracing is disabled.

    The factory in ``agents.llm`` calls this on each ``_build_ollama`` /
    ``_build_claude`` and appends the returned handler (alongside the
    Prom metrics callback) to the model's intrinsic ``callbacks`` list.
    Intrinsic-not-with_config is the only pattern that survives
    ``with_structured_output()`` chain wrapping — see the docstring
    on ``LangGraphMetricsCallback`` for the empirical evidence.
    """
    if _langfuse_client is None:
        return None
    # The handler reads trace context from langgraph's run_id + carries
    # contextvars (task_id, agent) we already bound in api/inbox.py +
    # graphs/fleet.py — those land on the Langfuse trace as metadata.
    return LangfuseLangchainCallback()


def flush_langfuse() -> None:
    """Flush buffered Langfuse events. Call on graceful shutdown."""
    if _langfuse_client is None:
        return
    try:
        _langfuse_client.flush()
    except Exception as exc:  # best-effort
        _LANGFUSE_LOGGER.warning("langfuse flush failed: %s", exc)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "LangGraphMetricsCallback",
    "configure_structlog",
    "flush_langfuse",
    "get_logger",
    "init_langfuse",
    "langfuse_callback_handler",
    "langgraph_calls_total",
    "langgraph_cost_usd_total",
    "langgraph_llm_duration_seconds",
    "langgraph_tokens_total",
    "llm_timer",
    "metrics_text",
    "record_llm_call",
]
