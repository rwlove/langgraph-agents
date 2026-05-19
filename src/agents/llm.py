"""Per-agent LLM factory.

Routes each agent to the right model on the right service. Per-group fallback
for Spark→P40 degraded routing. Claude escalation when explicitly requested or
when the degraded-mode-escalation flag is on AND both local paths are down.

Group enum mirrors `plan.md` v55 — `local-p40` | `local-spark` | `claude`.
Agents are assigned to groups via `AGENT_GROUP`; light/mechanical agents go
to P40 (qwen2.5:7b), reasoning/structured-output agents go to Spark
(qwen2.5:32b), no agent defaults to Claude.

Health-tracker is HARD-PINNED to local-only — the memory constraint
(`feedback_health_data_stays_local`-spirit, captured in IDENTITY.md) forbids
sending health data to Anthropic regardless of escalation request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama
from pydantic import SecretStr

from agents.health import service_healthy
from agents.observability import LangGraphMetricsCallback
from agents.settings import get_settings

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from agents.settings import Settings
    from agents.state import AgentId


ModelGroup = Literal["local-p40", "local-spark", "claude"]


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


# Per-group concrete model name. Edit one line here to bump a group's model.
GROUP_MODELS: dict[ModelGroup, str] = {
    "local-p40": "qwen2.5:7b",
    "local-spark": "qwen2.5:32b",
    "claude": "",  # set per-call from settings.claude_model
}


# Per-agent default group. Light/mechanical → P40; reasoning/structured → Spark.
# Health-tracker is local-only by hard constraint (see module docstring).
AGENT_GROUP: dict[AgentId, ModelGroup] = {
    "triager": "local-p40",
    "reporter": "local-p40",
    "note-maker": "local-p40",
    "researcher": "local-p40",
    "errand-runner": "local-p40",
    "supervisor": "local-p40",
    "property-coordinator": "local-p40",
    "health-tracker": "local-p40",
    "doc-writer": "local-p40",
    "coder": "local-spark",
    "reviewer": "local-spark",
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
) -> BaseChatModel:
    """Return a chat model for `agent_id`.

    Routing rules:
    - health-tracker NEVER escalates to Claude (hard constraint); explicit
      escalate=True or group_override="claude" is silently downgraded to
      `local-p40`.
    - If `escalate=True` AND ANTHROPIC_API_KEY is set, return Claude.
    - Otherwise use the per-agent group from `AGENT_GROUP` (or `group_override`).
    - `local-spark`: fall back to `local-p40` if Spark unhealthy (degraded
      routing — qwen2.5:7b instead of 32b). If both local paths are down AND
      `degraded_mode_escalation_enabled=True`, escalate to Claude. Else raise
      `LocalOllamaUnavailable`.
    - `local-p40`: no Blackwell fallback (running light agents on the 32b model
      is wasted GPU time). If unhealthy AND degraded-mode-escalation is on,
      escalate to Claude. Else raise.
    - `claude` (explicit): requires ANTHROPIC_API_KEY; otherwise raises.
    """
    settings = get_settings()

    if agent_id == "health-tracker" and (escalate or group_override == "claude"):
        # Hard constraint — never sends health data to Claude.
        group_override = "local-p40"
        escalate = False

    group: ModelGroup = group_override or AGENT_GROUP[agent_id]

    if escalate and settings.anthropic_api_key:
        return _build_claude(settings, agent_id, "claude", temperature=temperature)

    if group == "claude":
        if not settings.anthropic_api_key:
            msg = f"agent {agent_id!r} requested Claude but ANTHROPIC_API_KEY is not set"
            raise RuntimeError(msg)
        return _build_claude(settings, agent_id, "claude", temperature=temperature)

    if group == "local-spark":
        if service_healthy(settings.ollama_spark_url):
            return _build_ollama(
                settings.ollama_spark_url,
                GROUP_MODELS["local-spark"],
                agent_id=agent_id,
                effective_group="local-spark",
                temperature=temperature,
            )
        # Spark unhealthy — degrade to P40 (quality loss, no escalation).
        # effective_group="local-p40" so the metric label reflects what's
        # actually serving the request, not what was requested.
        if service_healthy(settings.ollama_p40_url):
            return _build_ollama(
                settings.ollama_p40_url,
                GROUP_MODELS["local-p40"],
                agent_id=agent_id,
                effective_group="local-p40",
                temperature=temperature,
            )
        if settings.degraded_mode_escalation_enabled and settings.anthropic_api_key:
            return _build_claude(settings, agent_id, "claude", temperature=temperature)
        raise LocalOllamaUnavailable(group, agent_id, failed_group="local-spark")

    # group == "local-p40"
    if service_healthy(settings.ollama_p40_url):
        return _build_ollama(
            settings.ollama_p40_url,
            GROUP_MODELS["local-p40"],
            agent_id=agent_id,
            effective_group="local-p40",
            temperature=temperature,
        )
    if settings.degraded_mode_escalation_enabled and settings.anthropic_api_key:
        return _build_claude(settings, agent_id, "claude", temperature=temperature)
    raise LocalOllamaUnavailable(group, agent_id, failed_group="local-p40")


def _build_ollama(
    base_url: str,
    model: str,
    *,
    agent_id: AgentId,
    effective_group: ModelGroup,
    temperature: float,
) -> BaseChatModel:
    """Build a ChatOllama client with metadata + metrics callback pre-bound.

    base_url should NOT contain /v1 suffix. Defensive: if a legacy /v1 suffix
    leaks in (e.g. someone sets ollama_p40_url to the old OpenAI-compat URL),
    strip it. ChatOllama uses Ollama's native /api routes, not the /v1 shim.

    `effective_group` is the group ACTUALLY serving (not necessarily the one
    requested via AGENT_GROUP) — a `local-spark` request that degraded to P40
    arrives here with `effective_group="local-p40"` so the metric labels
    reflect reality.
    """
    base_url = base_url.removesuffix("/v1").rstrip("/")
    base = ChatOllama(model=model, base_url=base_url, temperature=temperature)
    return _attach_observability(base, agent_id=agent_id, group=effective_group, model=model)


def _build_claude(
    settings: Settings,
    agent_id: AgentId,
    effective_group: ModelGroup,
    *,
    temperature: float,
) -> BaseChatModel:
    # ChatAnthropic init: `model` is the alias for `model_name`. api_key must
    # be SecretStr; callers ensure non-None via the escalate-path checks in llm().
    if settings.anthropic_api_key is None:
        msg = "ANTHROPIC_API_KEY required for Claude path but is None"
        raise RuntimeError(msg)
    base = ChatAnthropic(  # type: ignore[call-arg]
        model=settings.claude_model,
        api_key=SecretStr(settings.anthropic_api_key),
        temperature=temperature,
    )
    return _attach_observability(
        base, agent_id=agent_id, group=effective_group, model=settings.claude_model
    )


def _attach_observability(
    chat_model: BaseChatModel,
    *,
    agent_id: AgentId,
    group: ModelGroup,
    model: str,
) -> BaseChatModel:
    """Wrap the chat model with metadata + metrics callback pre-bound.

    Returns a RunnableBinding that still supports `.with_structured_output(...)`
    via attribute proxy — verified empirically. The bound `metadata` flows to
    `LangGraphMetricsCallback.on_llm_end` so the Prometheus counters get the
    correct labels per invocation without per-call-site config plumbing.

    Trigger label is intentionally NOT set here — the factory's `escalate=True`
    path and the degraded-mode-escalation path each emit their own concrete
    trigger via a wrapping `record_llm_call(...)` if needed. (For Phase 2
    rollout we don't trigger-tag yet; that lands in Phase 6 enablement.)
    """
    return chat_model.with_config(  # type: ignore[return-value]
        metadata={"agent": agent_id, "group": group, "model": model},
        callbacks=[LangGraphMetricsCallback()],
    )


__all__ = [
    "AGENT_GROUP",
    "GROUP_MODELS",
    "LocalOllamaUnavailable",
    "ModelGroup",
    "llm",
]
