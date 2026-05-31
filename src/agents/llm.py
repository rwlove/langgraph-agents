"""Per-agent LLM factory.

Routes each agent to the right model on the right service. Per-group fallback
for Spark→P40 degraded routing. Claude escalation when explicitly requested or
when the degraded-mode-escalation flag is on AND both local paths are down.

Group enum: `local-p40` | `local-spark` | `local-spark-coder` | `claude`.
Agents are assigned to groups via `AGENT_GROUP`; light/mechanical agents go
to P40 (qwen2.5:7b), reasoning/structured-output agents go to Spark general
(qwen2.5:32b), code-focused agents (`coder`, `reviewer`) go to Spark coder
(qwen2.5-coder:32b). No agent defaults to Claude.

`local-spark-coder` and `local-spark` share the same Ollama instance on
Spark — only the model name differs. Spark's MAX_LOADED_MODELS=3 lets both
sit resident so swap cost is negligible.

Health-tracker is HARD-PINNED to local-only — the memory constraint
(`feedback_health_data_stays_local`-spirit, captured in IDENTITY.md) forbids
sending health data to Anthropic regardless of escalation request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_ollama import ChatOllama
from pydantic import SecretStr

from agents.health import service_healthy
from agents.observability import (
    LangGraphMetricsCallback,
    agent_daily_spend_usd,
    global_claude_spend_usd,
    langfuse_callback_handler,
    langgraph_router_decision_total,
    task_spend_usd,
)
from agents.redaction import assert_emission_allowed
from agents.router import score_route
from agents.settings import get_settings

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from agents.settings import Settings
    from agents.state import AgentId


ModelGroup = Literal["local-p40", "local-spark", "local-spark-coder", "claude"]


class LocalOllamaUnavailable(RuntimeError):
    """Raised when the requested local-Ollama group (and any fallback) is unhealthy.

    Carries:
    - `group`: the originally-requested group from `AGENT_GROUP`.
    - `failed_group`: the group whose health-check actually failed last (same
      as `group` if no fallback was attempted, or `local-p40` if a Spark-down
      fallback path also failed).
    - `agent_id`: the agent the factory was building for.

    The /inbox API catches this and writes the task to the Postgres queue with
    `status=awaiting_ollama_recovery, failed_group=...` so a background poller
    (see `agents.queue`) can retry when health restores.
    """

    def __init__(
        self,
        group: ModelGroup,
        agent_id: AgentId,
        *,
        failed_group: ModelGroup | None = None,
    ) -> None:
        self.group: ModelGroup = group
        self.agent_id: AgentId = agent_id
        self.failed_group: ModelGroup = failed_group or group
        super().__init__(
            f"local-Ollama group {self.failed_group!r} unhealthy for agent {agent_id!r}"
        )


CapKind = Literal["global_daily", "per_task", "per_agent_daily"]


class CostCapHit(RuntimeError):
    """Raised when constructing a Claude client would cross any configured cost cap.

    The factory checks three caps in priority order BEFORE constructing
    the ``ChatAnthropic`` instance:

    1. ``global_daily`` — sum of ``langgraph_cost_usd_total{group="claude"}``
       across the whole process vs ``settings.cost_cap_global_daily_usd``.
    2. ``per_agent_daily`` — sum for ``(agent, today UTC)`` from the
       in-process ``_agent_daily_spend`` dict vs
       ``settings.cost_cap_per_agent_daily_usd``.
    3. ``per_task`` — sum for ``task_id`` (read from structlog
       contextvars) from the in-process ``_task_spend`` dict vs
       ``settings.cost_cap_per_task_usd``. Skipped silently when no
       ``task_id`` contextvar is bound at the call site, which is the
       documented graceful degradation for scheduled-job paths.

    Raising here is structurally the same as ``LocalOllamaUnavailable``
    — the request returns a typed error the caller can map to a queue
    state / user-visible message.

    Carries:
    - ``cap_kind``: which cap fired (``global_daily | per_task |
      per_agent_daily``). Lets the caller map to different remediation
      paths (e.g. "you've blown your daily" vs "this single task ate
      its budget — split it").
    - ``cap_usd``: the configured cap that was crossed.
    - ``accumulated_usd``: the in-process accumulator value at the time
      of the check (process-local; see ``global_claude_spend_usd`` for
      caveats; per-task + per-agent-daily share the same caveats).
    - ``agent_id``: the agent the factory was building for.
    """

    def __init__(
        self,
        *,
        cap_kind: CapKind,
        cap_usd: float,
        accumulated_usd: float,
        agent_id: AgentId,
    ) -> None:
        self.cap_kind: CapKind = cap_kind
        self.cap_usd = cap_usd
        self.accumulated_usd = accumulated_usd
        self.agent_id: AgentId = agent_id
        super().__init__(
            f"Claude cost cap hit ({cap_kind}) for agent {agent_id!r}: "
            f"accumulated ${accumulated_usd:.4f} USD >= cap ${cap_usd:.2f} USD"
        )


# Per-group concrete model name. Edit one line here to bump a group's model.
GROUP_MODELS: dict[ModelGroup, str] = {
    "local-p40": "qwen2.5:7b",
    "local-spark": "qwen2.5:32b",
    "local-spark-coder": "qwen2.5-coder:32b",
    "claude": "",  # set per-call from settings.claude_model
}


# Per-agent default group. Light/mechanical → P40; reasoning/structured → Spark.
# Health-tracker is local-only by hard constraint (see module docstring).
AGENT_GROUP: dict[AgentId, ModelGroup] = {
    "triager": "local-p40",
    "historian": "local-spark",
    "note-maker": "local-p40",
    "researcher": "local-spark",
    "errand-runner": "local-p40",
    "supervisor": "local-spark",
    "property-coordinator": "local-p40",
    "health-tracker": "local-p40",
    "doc-writer": "local-p40",
    "coder": "local-spark-coder",
    "reviewer": "local-spark-coder",
    "homelab-engineer": "local-spark",
    "network-operator": "local-spark",
    "storage-operator": "local-spark",
    "smart-home-operator": "local-spark",
    "ml-operator": "local-spark",
    # Reporter renders rich text for every user-facing DM — needs nuance and
    # link-formatting precision that the small P40 model can't reliably produce.
    "reporter": "local-spark",
    # Artist composes diffusion prompts + picks workflows — benefits from
    # Spark's better prompt-engineering performance.
    "artist": "local-spark",
    # Security needs nuance in distinguishing observation vs inference.
    "security": "local-spark",
    # Auditor needs precision in citing CVEs + scoring exposure.
    "auditor": "local-spark",
    "observability-operator": "local-spark",
}


def _claude_allowed(settings: Settings) -> bool:
    """Master gate for any Claude call.

    Both conditions are required: the ``ENABLE_CLAUDE_API`` kill switch is on
    AND a key is present. Every Claude routing gate in ``llm()`` checks this so
    a single ``ENABLE_CLAUDE_API=false`` pins the fleet 100% local without
    touching the key. ``_build_claude`` re-checks both as a hard backstop.
    """
    return settings.enable_claude_api and settings.anthropic_api_key is not None


def llm(  # noqa: PLR0911 — explicit returns map 1:1 to documented routing branches
    agent_id: AgentId,
    *,
    group_override: ModelGroup | None = None,
    escalate: bool = False,
    temperature: float = 0.2,
    trigger: str = "",
) -> BaseChatModel:
    """Return a chat model for `agent_id`.

    Routing rules:
    - ENABLE_CLAUDE_API is the master kill switch: when false, EVERY Claude path
      below is refused (escalation degrades to local; an explicit `group="claude"`
      request raises) regardless of ANTHROPIC_API_KEY. See `_claude_allowed`.
    - health-tracker NEVER escalates to Claude (hard constraint); explicit
      escalate=True or group_override="claude" is silently downgraded to
      `local-p40`.
    - If `escalate=True` AND Claude is allowed (key set + master switch on),
      return Claude.
    - Otherwise use the per-agent group from `AGENT_GROUP` (or `group_override`).
    - `local-spark` / `local-spark-coder`: fall back to `local-p40` if Spark
      unhealthy (degraded routing — qwen2.5:7b instead of the 32b general or
      coder model). If both local paths are down AND
      `degraded_mode_escalation_enabled=True`, escalate to Claude. Else raise
      `LocalOllamaUnavailable`.
    - `local-p40`: no Blackwell fallback (running light agents on the 32b model
      is wasted GPU time). If unhealthy AND degraded-mode-escalation is on,
      escalate to Claude. Else raise.
    - `claude` (explicit): requires ANTHROPIC_API_KEY; otherwise raises.

    `trigger` propagates through to ``LangGraphMetricsCallback`` as the
    `trigger` label on `langgraph_calls_total`. Default empty string means
    "no trigger override" (matches the /inbox + scheduled-graph paths).
    The OpenWebUI surface passes `trigger="openwebui"` so the dashboard panel
    filtering on that label still works after this surface was folded onto
    the factory.
    """
    settings = get_settings()

    if agent_id == "health-tracker" and (escalate or group_override == "claude"):
        # Hard constraint — never sends health data to Claude.
        group_override = "local-p40"
        escalate = False

    group: ModelGroup = group_override or AGENT_GROUP[agent_id]

    # Router scorer (HOMELAB-SPEC Layer 6) — the local-vs-Claude gate. Runs
    # after health-tracker is hard-pinned local (above) and after group
    # resolution, so it can only ever flip a local group to Claude, never the
    # reverse. The decision is always recorded; we only act on it when an API
    # key is present, so a "no key" cluster degrades to local exactly as today.
    decision = score_route(agent_id, group, settings)
    langgraph_router_decision_total.labels(
        agent=agent_id,
        decision="escalate" if decision.escalate else "local",
        reason=decision.reason,
    ).inc()
    if decision.escalate and _claude_allowed(settings):
        escalate = True

    if escalate and _claude_allowed(settings):
        return _build_claude(settings, agent_id, "claude", temperature=temperature, trigger=trigger)

    if group == "claude":
        if not _claude_allowed(settings):
            reason = (
                "ENABLE_CLAUDE_API is false"
                if not settings.enable_claude_api
                else "ANTHROPIC_API_KEY is not set"
            )
            msg = f"agent {agent_id!r} requested Claude but {reason}"
            raise RuntimeError(msg)
        return _build_claude(settings, agent_id, "claude", temperature=temperature, trigger=trigger)

    if group in ("local-spark", "local-spark-coder"):
        if service_healthy(settings.ollama_spark_url):
            return _build_ollama(
                settings.ollama_spark_url,
                GROUP_MODELS[group],
                agent_id=agent_id,
                effective_group=group,
                temperature=temperature,
                trigger=trigger,
            )
        # Spark unhealthy — degrade to P40 (quality loss, no escalation).
        # effective_group="local-p40" so the metric label reflects what's
        # actually serving the request, not what was requested. Coder-flavored
        # requests degrade to the same P40 7b general model — qwen2.5:7b's
        # coding ability is weak, but at least the request doesn't fail.
        if service_healthy(settings.ollama_p40_url):
            return _build_ollama(
                settings.ollama_p40_url,
                GROUP_MODELS["local-p40"],
                agent_id=agent_id,
                effective_group="local-p40",
                temperature=temperature,
                trigger=trigger,
            )
        if settings.degraded_mode_escalation_enabled and _claude_allowed(settings):
            return _build_claude(
                settings, agent_id, "claude", temperature=temperature, trigger=trigger
            )
        raise LocalOllamaUnavailable(group, agent_id, failed_group=group)

    # group == "local-p40"
    if service_healthy(settings.ollama_p40_url):
        return _build_ollama(
            settings.ollama_p40_url,
            GROUP_MODELS["local-p40"],
            agent_id=agent_id,
            effective_group="local-p40",
            temperature=temperature,
            trigger=trigger,
        )
    if settings.degraded_mode_escalation_enabled and _claude_allowed(settings):
        return _build_claude(settings, agent_id, "claude", temperature=temperature, trigger=trigger)
    raise LocalOllamaUnavailable(group, agent_id, failed_group="local-p40")


def _build_ollama(
    base_url: str,
    model: str,
    *,
    agent_id: AgentId,
    effective_group: ModelGroup,
    temperature: float,
    trigger: str = "",
) -> BaseChatModel:
    """Build a ChatOllama client with the metrics callback pre-attached.

    base_url should NOT contain /v1 suffix. Defensive: if a legacy /v1 suffix
    leaks in (e.g. someone sets ollama_p40_url to the old OpenAI-compat URL),
    strip it. ChatOllama uses Ollama's native /api routes, not the /v1 shim.

    `effective_group` is the group ACTUALLY serving (not necessarily the one
    requested via AGENT_GROUP) — a `local-spark` request that degraded to P40
    arrives here with `effective_group="local-p40"` so the metric labels
    reflect reality.

    `trigger` is propagated to the metrics callback's `trigger` label. Empty
    string by default; the OpenWebUI surface passes "openwebui" so dashboard
    panels filtering on that label keep working after the surface was folded
    onto this factory.

    Callback is attached as an INTRINSIC model callback (the model's own
    `callbacks` field) — survives `with_structured_output()` chain wrapping.
    Verified empirically that `with_config(callbacks=[...])` does NOT survive
    `with_structured_output`: the resulting RunnableSequence drops the
    callback, so we don't use that pattern.
    """
    base_url = base_url.removesuffix("/v1").rstrip("/")
    callbacks: list[BaseCallbackHandler] = [
        LangGraphMetricsCallback(
            agent=agent_id, group=effective_group, model=model, trigger=trigger
        ),
    ]
    lf = langfuse_callback_handler(agent_id)
    if lf is not None:
        callbacks.append(lf)
    return ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
        num_ctx=get_settings().ollama_num_ctx,
        callbacks=callbacks,
    )


def _build_claude(
    settings: Settings,
    agent_id: AgentId,
    effective_group: ModelGroup,
    *,
    temperature: float,
    trigger: str = "",
) -> BaseChatModel:
    # ChatAnthropic init: `model` is the alias for `model_name`. api_key must
    # be SecretStr; callers ensure non-None via the escalate-path checks in llm().
    if settings.anthropic_api_key is None:
        msg = "ANTHROPIC_API_KEY required for Claude path but is None"
        raise RuntimeError(msg)

    # Master kill-switch hard backstop. Every llm() routing gate already checks
    # _claude_allowed(), so this is unreachable in normal flow — it guarantees
    # that even a future direct _build_claude caller cannot bypass
    # ENABLE_CLAUDE_API=false.
    if not settings.enable_claude_api:
        msg = "ENABLE_CLAUDE_API is false but a Claude path was reached"
        raise RuntimeError(msg)

    # Phase 3.H — data-tier gate. Restricted-tier tasks never escalate
    # to a remote model. Raises `RestrictedTierEmissionBlocked` which
    # the calling node catches as a normal exception (state.output gets
    # an apologetic stub, the task ends without Claude being touched).
    # Reads `data_tier` from structlog contextvars; default 'internal'
    # if unbound, which lets old callers continue to escalate.
    assert_emission_allowed("claude")

    # Cost caps (P3.1 + follow-up). All three caps fire BEFORE
    # constructing the ChatAnthropic client so a single over-cap call
    # can't spend more on top of an already-blown budget.
    #
    # Checked in priority order (broadest → most-specific) so the
    # raised ``cap_kind`` reflects the first cap that actually trips:
    #
    # 1. global_daily — sum of ``langgraph_cost_usd_total{group=claude}``;
    #    see ``observability.global_claude_spend_usd`` for caveats around
    #    pod restart + the lack of a true 24h window.
    # 2. per_agent_daily — sum from the in-process per-agent-daily
    #    accumulator keyed by ``(agent, today UTC)``. Resets at UTC
    #    midnight (the YYYY-MM-DD key rolls over) and on pod restart.
    # 3. per_task — sum from the in-process per-task accumulator keyed
    #    by ``task_id``. Read from structlog contextvars; SKIPPED
    #    SILENTLY when no ``task_id`` is bound at this call site (e.g.
    #    scheduled-job paths). The global + per-agent caps still apply,
    #    which is the documented graceful degradation.
    global_spend = global_claude_spend_usd()
    if global_spend >= settings.cost_cap_global_daily_usd:
        raise CostCapHit(
            cap_kind="global_daily",
            cap_usd=settings.cost_cap_global_daily_usd,
            accumulated_usd=global_spend,
            agent_id=agent_id,
        )
    agent_spend = agent_daily_spend_usd(agent_id)
    if agent_spend >= settings.cost_cap_per_agent_daily_usd:
        raise CostCapHit(
            cap_kind="per_agent_daily",
            cap_usd=settings.cost_cap_per_agent_daily_usd,
            accumulated_usd=agent_spend,
            agent_id=agent_id,
        )
    bound_task_id = structlog.contextvars.get_contextvars().get("task_id")
    task_id = bound_task_id if isinstance(bound_task_id, str) else None
    if task_id is not None:
        task_spend = task_spend_usd(task_id)
        if task_spend >= settings.cost_cap_per_task_usd:
            raise CostCapHit(
                cap_kind="per_task",
                cap_usd=settings.cost_cap_per_task_usd,
                accumulated_usd=task_spend,
                agent_id=agent_id,
            )

    callbacks: list[BaseCallbackHandler] = [
        LangGraphMetricsCallback(
            agent=agent_id,
            group=effective_group,
            model=settings.claude_model,
            trigger=trigger,
        ),
    ]
    lf = langfuse_callback_handler(agent_id)
    if lf is not None:
        callbacks.append(lf)
    return ChatAnthropic(  # type: ignore[call-arg]
        model=settings.claude_model,
        api_key=SecretStr(settings.anthropic_api_key),
        temperature=temperature,
        callbacks=callbacks,
    )


__all__ = [
    "AGENT_GROUP",
    "GROUP_MODELS",
    "CapKind",
    "CostCapHit",
    "LocalOllamaUnavailable",
    "ModelGroup",
    "llm",
]
