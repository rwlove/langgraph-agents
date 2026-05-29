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
- A ``get_logger(component)`` factory returning a `structlog` BoundLogger
  pre-bound with a ``component`` field naming the source-code area
  (e.g. ``api.inbox``, ``graphs.fleet``). The ``agent`` field is
  reserved for per-event binding via ``structlog.contextvars`` so the
  triager / coder / reviewer label survives across node boundaries.

The label set is the v20-locked schema and shared across phases 2/4/6.
Phase 2's LLM factory will be the primary emitter; phase 6's Claude leg
adds ``trigger=requires_cloud|degraded_mode|policy_allowlist`` to the
``calls_total`` counter via the same handler.

Anthropic pricing is encoded in ``_ANTHROPIC_PRICING`` (USD per 1M tokens).
The ``LangGraphMetricsCallback`` uses it to populate ``langgraph_cost_usd_total``
on every Claude call. Local Ollama calls emit cost_usd=0 (no token price).
Update the table when Anthropic publishes new prices.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler as LangfuseLangchainCallback
from langfuse.types import TraceContext
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from ulid import ULID

from agents.settings import get_settings

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult


_LANGFUSE_LOGGER = logging.getLogger("agents.observability.langfuse")
_langfuse_client: Langfuse | None = None

# ---------------------------------------------------------------------------
# Anthropic pricing table (USD per 1 million tokens).
#
# Keys are model name prefixes — matched via str.startswith() so minor
# version suffixes (e.g. "claude-opus-4-7", "claude-sonnet-4-5") fall
# through correctly. Entries are (input_price, output_price) in USD/MTok.
#
# Source: https://www.anthropic.com/pricing (checked 2026-05-25).
# Update this table when Anthropic publishes new prices; the change is a
# one-liner here, no schema change needed.
# ---------------------------------------------------------------------------

_ANTHROPIC_PRICING: list[tuple[str, float, float]] = [
    # (model_prefix, input_usd_per_mtok, output_usd_per_mtok)
    ("claude-opus-4", 15.0, 75.0),
    ("claude-opus-3", 15.0, 75.0),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-sonnet-3-7", 3.0, 15.0),
    ("claude-sonnet-3-5", 3.0, 15.0),
    ("claude-haiku-3-5", 0.8, 4.0),
    ("claude-haiku-3", 0.25, 1.25),
]


def _compute_anthropic_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Return USD cost for an Anthropic API call using the published pricing table.

    Matches on the longest prefix in ``_ANTHROPIC_PRICING`` that the given
    ``model`` string starts with. Returns 0.0 for unrecognised models (e.g.
    Ollama local models, or a new Claude model not yet in the table) so
    callers don't have to guard against it.

    The function is intentionally pure (no I/O, no mutation) so it's safe to
    call from the metrics callback's hot path.
    """
    for prefix, input_price, output_price in _ANTHROPIC_PRICING:
        if model.startswith(prefix):
            return (tokens_in * input_price + tokens_out * output_price) / 1_000_000
    return 0.0


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

# Updated by ``agents.paused_threads.sweep_paused_threads`` on every
# sweep (startup lifecycle hook + /admin/paused-threads endpoint).
# Label values are strings ("true" / "false") because Prometheus
# label values can't be booleans.
#
# Operator alert: stale=="true" > 0 sustained → operator should check
# /admin/paused-threads for thread IDs awaiting an /approval verdict.
langgraph_paused_threads = Gauge(
    "langgraph_paused_threads",
    "Number of LangGraph threads currently paused at an interrupt, by staleness.",
    ("is_stale",),
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


def get_logger(component: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog BoundLogger pre-bound with a ``component`` field.

    ``component`` names the source-code area emitting the event
    (e.g. ``api.inbox``, ``graphs.fleet``, ``paused_threads``). When
    omitted, the logger is bound with ``component=agents`` for
    backward-compatibility with the top-level package.

    The ``agent`` field (triager / coder / reviewer / etc.) is
    deliberately NOT bound here — it must be set via
    ``structlog.contextvars.bind_contextvars(agent=...)`` from the node
    actually executing, so events emitted from shared helpers
    (graph wrappers, API surfaces, sweepers) inherit the correct
    per-task agent label. Binding ``agent`` at logger-construction
    time would override the contextvar (logger-level bindings outrank
    contextvars in structlog's merge order), which would silently
    mislabel every event a node emits through one of those shared
    helpers — observed and fixed in v0.2.24.
    """
    log = structlog.get_logger("agents")
    bindings: dict[str, Any] = {"component": component or "agents"}
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
        langgraph_tokens_total.labels(agent=agent, group=group, model=model, direction="in").inc(
            tokens_in
        )
    if tokens_out:
        langgraph_tokens_total.labels(agent=agent, group=group, model=model, direction="out").inc(
            tokens_out
        )
    if cost_usd:
        langgraph_cost_usd_total.labels(agent=agent, group=group, model=model).inc(cost_usd)
        # Per-task + per-agent-daily accumulators (P3.1 follow-up). The
        # task_id is read from structlog contextvars (bound by /inbox +
        # graphs/fleet at request entry); when it's absent — e.g. a
        # scheduled-job path that never bound one — the per-task spend
        # is silently skipped. Per-agent-daily always updates because
        # `agent` is a known argument here. See the eviction note on
        # ``_evict_old_agent_daily``.
        task_id = structlog.contextvars.get_contextvars().get("task_id")
        record_task_spend(
            task_id=task_id if isinstance(task_id, str) else None,
            agent=agent,
            cost_usd=cost_usd,
        )
    if duration_seconds:
        langgraph_llm_duration_seconds.labels(agent=agent, group=group, model=model).observe(
            duration_seconds
        )


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


def global_claude_spend_usd() -> float:
    """Return the in-process accumulated Claude spend, in USD.

    Sums the ``langgraph_cost_usd_total`` Counter across every label
    combination where ``group="claude"``. This is the value the
    cost-cap guard in ``agents.llm._build_claude`` checks before
    handing out a ``ChatAnthropic`` instance.

    Caveats:

    - **Process-local.** The Counter lives in this process's
      ``prometheus_client`` default registry. There's no Redis /
      Postgres aggregate; another replica wouldn't see this one's
      spend. We deploy with ``strategy: Recreate`` + a single
      replica, so concurrency isn't the problem.
    - **Resets on pod restart.** Same caveat as the
      ``/admin/costs/today`` dashboard panel — a rollout or crash
      zeroes the in-process counter and the cap effectively resets.
      For the global-daily cap that's been deemed acceptable: a
      restart-loop spending money in spikes is bounded by the
      restart-cost-per-cycle, not the daily cap. If the loop's
      spend-per-restart exceeds the cap, the alarm is the restart
      loop, not the cap.
    - **No window.** The Counter accumulates monotonically over the
      process lifetime. There's no rolling 24h window on the
      in-process counter; the name "24h spend" applies in spirit
      because the pod is usually rolled around once a day for
      unrelated reasons. The Langfuse-backed dashboard remains the
      authoritative source of truth for actual cost over time.
    """
    total = 0.0
    for metric in langgraph_cost_usd_total.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels.get("group") == "claude":
                total += sample.value
    return total


# ---------------------------------------------------------------------------
# Per-task + per-agent-daily cost accumulators (P3.1 follow-up).
#
# Process-local dicts updated from ``record_llm_call`` whenever a
# non-zero cost is stamped. Same caveats as the global-daily cap
# (process-local; resets on pod restart; no cross-replica view —
# Recreate strategy + single replica makes that acceptable). Keys:
#
# - ``_task_spend[task_id]`` → float USD across that task's lifetime
#   (the lifetime is "while the process is up"; restart wipes it,
#   which is also when a paused thread would be resumed via the
#   /admin/paused-threads sweep, so resuming threads start at zero).
# - ``_agent_daily_spend[(agent, "YYYY-MM-DD" UTC)]`` → float USD
#   for that agent on that UTC day.
#
# Per-task is keyed by task_id (a string from the request entry's
# structlog contextvar bind). When the contextvar isn't bound — e.g.
# scheduled-job paths that never call ``structlog.contextvars.bind_
# contextvars(task_id=...)`` — ``record_task_spend`` skips the per-
# task update silently. The per-agent-daily update is always applied
# because ``agent`` is a required argument.
# ---------------------------------------------------------------------------

_task_spend: dict[str, float] = {}
_agent_daily_spend: dict[tuple[str, str], float] = {}

# Drop ``_agent_daily_spend`` entries older than this many days at the
# start of each ``record_task_spend`` call. Without eviction the dict
# would grow by ``len(AGENT_GROUP)`` entries per UTC day forever (small
# in absolute terms — ~17 agents x N days — but unbounded is unbounded).
# 7 days is generous enough that operator queries via getters can
# meaningfully ask about earlier UTC days within a week-long debugging
# window; older than that, the answer is "spend was 0 (evicted)".
_AGENT_DAILY_RETENTION_DAYS = 7


def _today_utc_key() -> str:
    """Return today's UTC date as ``YYYY-MM-DD`` (the per-agent-daily key)."""
    return datetime.now(UTC).date().isoformat()


def _evict_old_agent_daily() -> None:
    """Drop ``_agent_daily_spend`` entries older than the retention window.

    O(n) over the dict size; n is bounded by ``len(agents) x retention_days``
    in steady state, so the scan cost is trivial. Called from
    ``record_task_spend`` (the only writer) so eviction stays inline with
    the writes that grow the dict.
    """
    cutoff = datetime.now(UTC).date() - timedelta(days=_AGENT_DAILY_RETENTION_DAYS)
    stale = [key for key in _agent_daily_spend if date.fromisoformat(key[1]) < cutoff]
    for key in stale:
        del _agent_daily_spend[key]


def record_task_spend(
    *,
    task_id: str | None,
    agent: str,
    cost_usd: float,
) -> None:
    """Update both per-task and per-agent-daily accumulators.

    Called from ``record_llm_call`` whenever ``cost_usd > 0``. When
    ``task_id`` is ``None`` (no contextvar bound at emission time),
    the per-task accumulator is skipped silently — the global +
    per-agent-daily caps still apply, which is the documented graceful
    degradation for scheduled-job paths.

    Always evicts stale per-agent-daily entries before writing — keeps
    the dict bounded without a background reaper.
    """
    if cost_usd <= 0:
        return
    _evict_old_agent_daily()
    if task_id is not None:
        _task_spend[task_id] = _task_spend.get(task_id, 0.0) + cost_usd
    agent_key = (agent, _today_utc_key())
    _agent_daily_spend[agent_key] = _agent_daily_spend.get(agent_key, 0.0) + cost_usd


def task_spend_usd(task_id: str | None) -> float:
    """Return cumulative spend for ``task_id``, or 0.0 if untracked/unknown.

    ``None`` returns 0.0 — used by the cap check in ``llm._build_claude``
    when no ``task_id`` contextvar is bound, which skips the per-task cap.
    """
    if task_id is None:
        return 0.0
    return _task_spend.get(task_id, 0.0)


def agent_daily_spend_usd(agent: str) -> float:
    """Return ``agent``'s cumulative spend for the current UTC day."""
    return _agent_daily_spend.get((agent, _today_utc_key()), 0.0)


def _reset_cost_accumulators() -> None:
    """Test-only: zero out the per-task + per-agent-daily dicts.

    Not part of the public API — invoked by the test fixture in
    ``tests/test_cost_cap.py`` to keep tests independent. Production
    code should not call this.
    """
    _task_spend.clear()
    _agent_daily_spend.clear()


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
            usage = response.llm_output.get("token_usage") or response.llm_output.get("usage") or {}
            # Anthropic: input_tokens/output_tokens; Ollama/OpenAI: prompt_tokens/completion_tokens
            tokens_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)

        # Compute cost for Anthropic models using the published pricing table.
        # Local Ollama models return 0.0 (no token price); unrecognised model
        # strings also return 0.0 safely. Cost feeds langgraph_cost_usd_total
        # which drives cost caps in llm._build_claude + the Grafana dashboard.
        cost_usd = _compute_anthropic_cost(self.model, tokens_in, tokens_out)

        record_llm_call(
            agent=self.agent,
            group=self.group,
            model=self.model,
            outcome="success",
            trigger=self.trigger,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
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
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    ):
        _LANGFUSE_LOGGER.info("langfuse keys not configured; per-task tracing disabled")
        return

    _langfuse_client = Langfuse(
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )
    _LANGFUSE_LOGGER.info("langfuse client initialized (host=%s)", settings.langfuse_host)


def langfuse_callback_handler(agent_id: str | None = None) -> BaseCallbackHandler | None:
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
    ctx = structlog.contextvars.get_contextvars()
    task_id = ctx.get("task_id")
    # Pin the LangChain trace to the task_id so all LLM calls within one
    # task share a single Langfuse trace rather than scattering as anonymous
    # orphan traces. Trace name comes from the LangChain run name (model
    # class / chain name) — that's the limit of the v3 LangChain integration.
    #
    # Langfuse v3 requires a 32-char lowercase hex trace ID (UUID4 hex
    # format). task_id is a ULID (26-char Crockford base32) — convert via
    # the 128-bit integer representation that both encodings share.
    trace_id: str | None = None
    if task_id:
        try:
            trace_id = UUID(int=int(ULID.from_str(str(task_id)))).hex
        except Exception:
            _LANGFUSE_LOGGER.warning(
                "langfuse_trace_id_conversion_failed",
                task_id=task_id,
            )
    trace_context: TraceContext | None = TraceContext(trace_id=trace_id) if trace_id else None
    return LangfuseLangchainCallback(trace_context=trace_context)


def flush_langfuse() -> None:
    """Flush buffered Langfuse events. Call on graceful shutdown."""
    if _langfuse_client is None:
        return
    try:
        _langfuse_client.flush()
    except Exception as exc:  # best-effort
        _LANGFUSE_LOGGER.warning("langfuse flush failed: %s", exc)


# ---------------------------------------------------------------------------
# OpenTelemetry tracing (Phase 3.K).
#
# Distributed trace pipeline: lga emits OTLP gRPC → in-cluster
# opentelemetry-collector (deployed 2.J2) → Tempo (deployed 2.J1) →
# Grafana datasource.
#
# Sibling-to-Langfuse: Langfuse owns the per-LLM-call trace surface
# (run_id, prompt/response payloads, cost). OTel owns the per-request
# distributed trace (the journey of a /inbox task through node calls +
# MCP tool calls + outbound HTTP). They coexist; neither replaces the
# other.
#
# Silent-disable: if OTLP collector unreachable, the OTel SDK queues
# spans in-memory and drops on backpressure. /inbox never blocks on
# trace export.
# ---------------------------------------------------------------------------

_OTEL_LOGGER = logging.getLogger("agents.observability.otel")
_otel_initialized = False


def init_otel() -> None:
    """Initialize global OTel tracer provider + OTLP exporter.

    Idempotent. Reads:
    - `OTEL_EXPORTER_OTLP_ENDPOINT` (env, default
      `http://opentelemetry-collector.observability.svc.cluster.local:4317`)
    - `OTEL_SERVICE_NAME` (env, default `langgraph-agents`)
    - `OTEL_ENABLED` (env, default `true`)

    Side effects on first successful call:
    - Sets the global TracerProvider.
    - Attaches `OTLPSpanExporter` via `BatchSpanProcessor`.
    - Auto-instruments `httpx` so outgoing MCP / Ollama / Claude / ntfy
      calls propagate W3C `traceparent` headers automatically.
    - The FastAPI app itself is instrumented in `agents.main` after the
      `FastAPI` instance is constructed (see `instrument_fastapi_app`).
    """
    global _otel_initialized  # noqa: PLW0603
    if _otel_initialized:
        return

    if os.getenv("OTEL_ENABLED", "true").lower() not in ("true", "1", "yes"):
        _OTEL_LOGGER.info("OTEL_ENABLED=false; tracing disabled")
        return

    try:
        endpoint = os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://opentelemetry-collector.observability.svc.cluster.local:4317",
        )
        service_name = os.getenv("OTEL_SERVICE_NAME", "langgraph-agents")

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "ai",
            }
        )
        provider = TracerProvider(resource=resource)
        # `insecure=True` because the in-cluster collector accepts plain
        # gRPC; Cilium handles transport-level isolation via CNP. If we
        # ever expose a remote collector, switch to TLS here.
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        HTTPXClientInstrumentor().instrument()

        _otel_initialized = True
        _OTEL_LOGGER.info(
            "OTel tracer initialized: service=%s endpoint=%s",
            service_name,
            endpoint,
        )
    except Exception as exc:  # best-effort init
        _OTEL_LOGGER.warning("OTel init failed (tracing disabled): %s", exc)


def instrument_fastapi_app(app: Any) -> None:
    """Auto-span every FastAPI request. Call from `agents.main` after
    constructing the `FastAPI` instance.

    Skipped silently if OTel init failed earlier (the global provider
    is the NoOp default and instrumentation harmlessly produces no
    spans).
    """
    if not _otel_initialized:
        return
    try:
        FastAPIInstrumentor.instrument_app(app)
        _OTEL_LOGGER.info("FastAPI instrumented for OTel spans")
    except Exception as exc:
        _OTEL_LOGGER.warning("FastAPI OTel instrumentation failed: %s", exc)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "LangGraphMetricsCallback",
    "_compute_anthropic_cost",
    "agent_daily_spend_usd",
    "configure_structlog",
    "flush_langfuse",
    "get_logger",
    "global_claude_spend_usd",
    "init_langfuse",
    "init_otel",
    "instrument_fastapi_app",
    "langfuse_callback_handler",
    "langgraph_calls_total",
    "langgraph_cost_usd_total",
    "langgraph_llm_duration_seconds",
    "langgraph_paused_threads",
    "langgraph_tokens_total",
    "llm_timer",
    "metrics_text",
    "record_llm_call",
    "record_task_spend",
    "task_spend_usd",
]
