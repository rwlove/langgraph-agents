"""Runtime configuration via environment variables.

Anything that differs between dev and prod lives here. Anything that's a secret
loads from env (provided by the cluster's ExternalSecret in deployment, or .env
locally).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- vault content ---
    vault_root: Path = Field(
        default=Path.home() / "vaults" / "claude",
        description="Root of the claude vault. In cluster this is a PVC mount.",
    )

    # --- model providers ---
    # ollama_base_url is the legacy single-endpoint setting (pre-Spark, OpenAI
    # /v1 shim URL). Phase 2 replaces it with explicit per-service URLs:
    # ollama_p40_url + ollama_spark_url. Both default to in-cluster Service
    # DNS without the /v1 suffix (langchain_ollama.ChatOllama uses native /api
    # routes; the factory strips /v1 defensively if it leaks in).
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        description=(
            "DEPRECATED — kept for backward compat. New code uses ollama_p40_url "
            "+ ollama_spark_url + the agents.llm factory. Will be removed after "
            "one release."
        ),
    )
    ollama_p40_url: str = Field(
        default="http://ollama.ai.svc.cluster.local:11434",
        description=(
            "P40-Ollama Service endpoint (qwen2.5:7b). Used for light/mechanical "
            "agents per AGENT_GROUP. NO /v1 suffix — ChatOllama uses native /api "
            "routes."
        ),
    )
    ollama_spark_url: str = Field(
        default="http://ollama-spark.ai.svc.cluster.local:11434",
        description=(
            "Spark-Ollama Service endpoint (qwen2.5:32b). Used for "
            "reasoning/structured-output agents per AGENT_GROUP."
        ),
    )
    anthropic_api_key: str | None = Field(
        default=None,
        description="Optional. Required only for agents that opt into Claude API.",
    )
    enable_claude_api: bool = Field(
        default=False,
        description=(
            "DEPRECATED master switch — kept for backward compat. New flow uses "
            "the per-call escalate= kwarg + degraded_mode_escalation_enabled "
            "flag in agents.llm."
        ),
    )
    degraded_mode_escalation_enabled: bool = Field(
        default=False,
        description=(
            "When True, the factory falls back to Claude if BOTH local Ollama "
            "endpoints are unhealthy AND ANTHROPIC_API_KEY is set. When False, "
            "LocalOllamaUnavailable is raised and the task queues in Postgres "
            "(agents.queue) for retry when health restores."
        ),
    )
    claude_model: str = Field(
        default="claude-opus-4-7",
        description=(
            "Claude model for escalation paths. Override via env. Used by "
            "agents.llm._build_claude."
        ),
    )
    triager_model: str = Field(
        default="qwen2.5:7b",
        description=(
            "DEPRECATED — model selected per-agent by AGENT_GROUP in agents.llm. "
            "Kept for backward compat with any direct reads."
        ),
    )
    specialist_model: str = Field(
        default="qwen2.5:7b",
        description=(
            "DEPRECATED — model selected per-agent by AGENT_GROUP in agents.llm. "
            "Kept for backward compat with any direct reads."
        ),
    )

    # --- state + memory ---
    postgres_url: str = Field(
        default="postgresql://localhost:5432/langgraph_checkpoints",
        description="Postgres connection string for LangGraph checkpointer.",
    )
    memory_postgres_url: str = Field(
        default="postgresql://localhost:5432/langgraph_memory",
        description="Postgres connection string for pgvector memory store.",
    )
    memory_backend: str = Field(
        default="postgres",
        description=(
            "BaseStore backend. 'postgres' uses MCPMemoryStore against the "
            "shared `kg.*` schema (cross-agent KG, see memory-mcp). 'none' "
            "disables long-term store, falling back to in-message scratchpad."
        ),
    )
    memory_embed_model: str = Field(
        default="nomic-embed-text",
        description=(
            "Embedding model for MCPMemoryStore. Must match the dim of "
            "kg.observations.embedding (1024 for bge-m3 post-Phase-A; 768 "
            "for nomic-embed-text pre-cutover)."
        ),
    )
    memory_ollama_url: str | None = Field(
        default=None,
        description=(
            "Optional Ollama endpoint for MCPMemoryStore embeddings. When "
            "None, falls back to ollama_p40_url. Set this to point at "
            "ollama-spark to avoid the P40 model-swap churn that the "
            "default path causes (bge-m3 + qwen2.5:7b alternating loads "
            "out of the P40's MAX_LOADED=1 slot)."
        ),
    )

    # --- tools ---
    mcp_gateway_url: str = Field(
        default="http://mcp-gateway-istio.mcp-system.svc.cluster.local:8080",
        description="MCP gateway base URL.",
    )

    # --- observability ---
    langfuse_host: str | None = Field(default=None)
    langfuse_public_key: str | None = Field(default=None)
    langfuse_secret_key: str | None = Field(default=None)

    # --- notifications ---
    pushover_app_token: str | None = Field(default=None)
    pushover_user_key: str | None = Field(default=None)

    # --- zulip reply-as-triager ---
    # When /inbox is called with source="zulip" and a zulip_user_id, the
    # handler posts the final output as a DM from the triager bot back
    # to that user. Empty values disable the post-back (best-effort).
    zulip_host: str | None = Field(
        default=None,
        description="Zulip realm host (e.g. chat.thesteamedcrab.com). No scheme.",
    )
    zulip_triager_email: str | None = Field(default=None)
    zulip_triager_api_key: str | None = Field(default=None)

    # --- approval signing (HMAC shared with n8n approval-broker) ---
    langgraph_approval_signing_key: str | None = Field(
        default=None,
        description=(
            "Shared HMAC secret for verifying approval tokens minted by n8n's "
            "approval-receive workflow. Must match $env.LANGGRAPH_APPROVAL_SIGNING_KEY "
            "in the n8n container. Both pods receive it from the same 1Password item "
            "via ExternalSecret."
        ),
    )

    # --- cost caps (per security review cat 7) ---
    # All three caps are enforced in `agents.llm._build_claude` BEFORE
    # constructing the ChatAnthropic client. Sources of truth:
    #
    # - global_daily: `langgraph_cost_usd_total{group="claude"}` Counter
    #   sum via `agents.observability.global_claude_spend_usd`.
    # - per_agent_daily: in-process `_agent_daily_spend[(agent, today UTC)]`
    #   via `agents.observability.agent_daily_spend_usd`. Keyed by
    #   YYYY-MM-DD UTC; rolls over at UTC midnight; evicts entries older
    #   than 7 days on each write.
    # - per_task: in-process `_task_spend[task_id]` via
    #   `agents.observability.task_spend_usd`. task_id is read from
    #   structlog contextvars (bound by /inbox + graphs/fleet at entry);
    #   SKIPPED silently when no task_id is bound at the call site
    #   (e.g. scheduled-job paths). The global + per-agent caps still
    #   apply, which is the documented graceful degradation.
    #
    # Same process-local caveats as global_daily: all three accumulators
    # reset on pod restart; cross-replica view is not aggregated (single-
    # replica `Recreate` deployment makes that acceptable).
    cost_cap_per_task_usd: float = 5.0
    cost_cap_per_agent_daily_usd: float = 10.0
    cost_cap_global_daily_usd: float = 30.0

    # --- startup sweep ---
    # P3.7's startup sweep walks the langgraph_checkpoints table to log
    # stale paused threads. Default OFF because (a) no node calls
    # `interrupt()` outside of errand-runner today so there's nothing
    # to sweep for, and (b) on an instance with days of checkpoint
    # history the walk starves the Postgres connection pool — /inbox
    # blocks waiting for a connection. Flip to True per-deployment via
    # env once class-C/D nodes routinely pause AND the operator wants
    # the startup diagnostic. The on-demand `/admin/paused-threads`
    # endpoint runs the sweep regardless.
    startup_sweep_enabled: bool = Field(default=False)

    # --- runtime ---
    log_level: str = "INFO"
    user_timezone: str = "America/New_York"

    @property
    def workspaces_dir(self) -> Path:
        return self.vault_root / "agents" / "workspaces"

    @property
    def skills_dir(self) -> Path:
        return self.vault_root / "agents" / "skills"

    @property
    def shared_workspace_dir(self) -> Path:
        return self.workspaces_dir / "_shared"


@cache
def get_settings() -> Settings:
    """Cached settings accessor. Override via env vars or .env file."""
    return Settings()
