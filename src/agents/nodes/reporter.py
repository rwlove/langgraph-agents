"""reporter — activity log curator + accomplishments promoter.

Phase 1 implementation: produce a digest from the per-agent activity logs
under `vault/agents/*/memory/activity-log.md`. The full TODO-2 backfill +
accomplishments-staging bridge lands later when more agents are actively
logging.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import FleetState
from agents.tools.obsidian import WriteResult, _write_atomic

_AGENT_ID = "reporter"
_MODEL = "qwen2.5:14b"
_TEMPERATURE = 0.2


class DailyDigest(BaseModel):
    notable: list[str] = Field(
        default_factory=list,
        description="Tier 2: completed tasks with a notable outcome.",
    )
    routine: list[str] = Field(
        default_factory=list,
        description="Tier 3: routine completions, triages, reconciles.",
    )
    awaiting_user: list[str] = Field(
        default_factory=list,
        description="Tasks paused at 30min / 4h / 7d tiers.",
    )
    cost_summary: str = Field(
        default="",
        description="Today's spend vs daily cap, plus weekly cumulative.",
    )


def _build_llm() -> BaseChatModel:
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(DailyDigest)  # type: ignore[return-value]


def _collect_activity_logs() -> str:
    """Concatenate the per-agent activity logs for the LLM to summarize.

    Each log lives at `vault/agents/<agent>/memory/activity-log.md`. Files
    that don't exist yet are silently skipped — phase 8 agents start writing
    their logs once they're authored.
    """
    settings = get_settings()
    agents_dir = settings.vault_root / "agents"
    if not agents_dir.is_dir():
        return "(no agent activity logs found)"

    chunks: list[str] = []
    for agent_dir in sorted(agents_dir.iterdir()):
        # Skip the workspaces/ + skills/ + _shared dirs (those aren't per-agent logs)
        if not agent_dir.is_dir() or agent_dir.name in {"workspaces", "skills", "_shared"}:
            continue
        log = agent_dir / "memory" / "activity-log.md"
        if not log.is_file():
            continue
        chunks.append(f"## {agent_dir.name}\n\n{log.read_text(encoding='utf-8')}")

    return "\n\n---\n\n".join(chunks) if chunks else "(no activity log entries today)"


def _render_markdown(digest: DailyDigest, day: date) -> str:
    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "_(none)_"

    return (
        "---\n"
        f"date: {day.isoformat()}\n"
        "kind: daily-digest\n"
        "---\n\n"
        f"# Daily digest — {day.isoformat()}\n\n"
        "## Tier 2 — notable\n\n"
        f"{_bullets(digest.notable)}\n\n"
        "## Tier 3 — routine\n\n"
        f"{_bullets(digest.routine)}\n\n"
        "## Awaiting user\n\n"
        f"{_bullets(digest.awaiting_user)}\n\n"
        "## Cost summary\n\n"
        f"{digest.cost_summary or '_(no cost recorded)_'}\n"
    )


def _write_daily_digest(day: date, body: str) -> WriteResult:
    settings = get_settings()
    path = settings.vault_root / "reports" / f"daily-{day.isoformat()}.md"
    n = _write_atomic(path, body)
    return WriteResult(path=path, bytes_written=n)


def reporter_node(state: FleetState) -> dict[str, Any]:
    """Aggregate per-agent activity into the daily digest.

    Triggered by the n8n schedule cron at 22:00 local. The inbox `content`
    is expected to be a marker like "generate today's digest" — the actual
    data source is the activity logs on disk.
    """
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()
    today = datetime.now(UTC).date()
    activity = _collect_activity_logs()

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"Date: {today.isoformat()}\n\n"
                "Aggregate the per-agent activity below into a DailyDigest. "
                "Group by tier (notable vs routine). Surface awaiting-user "
                "tasks with their tier (30min/4h/7d). Cost summary in one "
                "sentence (zero if no Claude API calls today).\n\n"
                f"PER-AGENT ACTIVITY LOGS:\n\n{activity}"
            )
        ),
    ]

    digest: DailyDigest = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(digest, today)
    result = _write_daily_digest(today, markdown)

    return {
        "output": (
            f"daily digest: {result.path} "
            f"(notable={len(digest.notable)}, routine={len(digest.routine)}, "
            f"awaiting={len(digest.awaiting_user)})"
        ),
    }
