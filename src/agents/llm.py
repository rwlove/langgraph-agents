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

from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_ollama import ChatOllama
from pydantic import SecretStr

from agents.health import service_healthy
from agents.observability import (
    LangGraphMetricsCallback,
    global_claude_spend_usd,
    langfuse_callback_handler,
)
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


class CostCapHit(RuntimeError):
    """Raised when constructing a Claude client would cross the global daily cap.

    The factory checks ``observability.global_claude_spend_usd()`` (sum of
    ``langgraph_cost_usd_total{group="claude"}``) against
    ``settings.cost_cap_global_daily_usd`` BEFORE constructing the
    ``ChatAnthropic`` instance. Raising here is structurally the same as
    ``LocalOllamaUnavailable`` — the request returns a typed error the
    caller can map to a queue state / user-visible message.

    Carries:
    - ``cap_usd``: the configured cap that was crossed.
    - ``accumulated_usd``: the in-process Counter sum at the time of the
      check (process-local; see ``global_claude_spend_usd`` for caveats).
    - ``agent_id``: the agent the factory was building for.

    Scoped to the GLOBAL DAILY cap only. ``cost_cap_per_task_usd`` and
    ``cost_cap_per_agent_daily_usd`` are still defined in settings but
    not yet enforced — they need ``FleetState.accumulated_cost_usd``
    plumbed through every node, which is a bigger refactor. Documented
    in ``settings.py`` next to those fields.
    """

    def __init__(
        self,
        *,
        cap_usd: float,
        accumulated_usd: float,
        agent_id: AgentId,
    ) -> None:
        self.cap_usd = cap_usd
        self.accumulated_usd = accumulated_usd
        self.agent_id: AgentId = agent_id
        super().__init__(
            f"Claude cost cap hit for agent {agent_id!r}: "
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
    "reporter": "local-spark",
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
    "observability-operator": "local-spark",
}


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
    - health-tracker NEVER escalates to Claude (hard constraint); explicit
      escalate=True or group_override="claude" is silently downgraded to
      `local-p40`.
    - If `escalate=True` AND ANTHROPIC_API_KEY is set, return Claude.
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

    if escalate and settings.anthropic_api_key:
        return _build_claude(
            settings, agent_id, "claude", temperature=temperature, trigger=trigger
        )

    if group == "claude":
        if not settings.anthropic_api_key:
            msg = f"agent {agent_id!r} requested Claude but ANTHROPIC_API_KEY is not set"
            raise RuntimeError(msg)
        return _build_claude(
            settings, agent_id, "claude", temperature=temperature, trigger=trigger
        )

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
        if settings.degraded_mode_escalation_enabled and settings.anthropic_api_key:
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
    if settings.degraded_mode_escalation_enabled and settings.anthropic_api_key:
        return _build_claude(
            settings, agent_id, "claude", temperature=temperature, trigger=trigger
        )
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
    lf = langfuse_callback_handler()
    if lf is not None:
        callbacks.append(lf)
    return ChatOllama(
        model=model,
        base_url=base_url,
        temperature=temperature,
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

    # Global daily cost cap (P3.1). Process-local Counter; see
    # ``observability.global_claude_spend_usd`` for the caveats around
    # pod restart + the lack of a true 24h window. The cap fires
    # BEFORE constructing the ChatAnthropic client so a single
    # over-cap call can't spend more on top of an already-blown
    # budget. Per-task and per-agent caps are NOT enforced here yet
    # — they require FleetState.accumulated_cost_usd plumbing; see
    # the comment in settings.py.
    accumulated = global_claude_spend_usd()
    if accumulated >= settings.cost_cap_global_daily_usd:
        raise CostCapHit(
            cap_usd=settings.cost_cap_global_daily_usd,
            accumulated_usd=accumulated,
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
    lf = langfuse_callback_handler()
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
    "CostCapHit",
    "LocalOllamaUnavailable",
    "ModelGroup",
    "llm",
]
