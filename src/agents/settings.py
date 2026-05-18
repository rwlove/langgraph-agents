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
    cost_cap_per_task_usd: float = 5.0
    cost_cap_per_agent_daily_usd: float = 10.0
    cost_cap_global_daily_usd: float = 30.0

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
