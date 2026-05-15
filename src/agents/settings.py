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
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Ollama OpenAI-compatible endpoint.",
    )
    anthropic_api_key: str | None = Field(
        default=None,
        description="Optional. Required only for agents that opt into Claude API.",
    )
    enable_claude_api: bool = Field(
        default=False,
        description="Master switch. Even if set, health-tracker never uses Claude.",
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
