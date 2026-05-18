"""reviewer — memory hygiene + TODO aging + drift detection.

Weekly cadence. Read-only-suggest: produces a digest of issues; never edits
memory directly. Architecturally blocked from the personal vault (the
ObsidianClient is constructed with the claude vault root only).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import AgentId, FleetState
from agents.tools.obsidian import WriteResult, _write_atomic

_AGENT_ID: AgentId = "reviewer"
_TEMPERATURE = 0.2

Tier = Literal["urgent", "notable", "routine"]


class TodoAgeReport(BaseModel):
    file_path: str
    last_modified: str  # ISO date
    tier: Tier
    suggestion: str = Field(description="archive | promote | unblock | leave-alone + 1-line why")


class DriftFinding(BaseModel):
    description: str
    files_involved: list[str]
    severity: Literal["high", "medium", "low"]


class DeadLink(BaseModel):
    source_file: str
    reference: str = Field(description="The [[name]] or path that doesn't resolve.")


class ReviewerDigest(BaseModel):
    aging_todos: list[TodoAgeReport] = Field(default_factory=list)
    drift_findings: list[DriftFinding] = Field(default_factory=list)
    dead_links: list[DeadLink] = Field(default_factory=list)
    summary: str = Field(description="One-paragraph state-of-the-vault.")


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(ReviewerDigest)  # type: ignore[return-value]


def _find_aging_todos() -> list[tuple[Path, datetime, Tier]]:
    """Walk projects/*/memory/project_todo_*.md and classify by age.

    7d → urgent, 30d → notable, 90d → routine. Older than that, ignore
    (probably truly dead and waiting for cleanup).
    """
    settings = get_settings()
    projects = settings.vault_root / "projects"
    if not projects.is_dir():
        return []

    now = datetime.now(UTC)
    out: list[tuple[Path, datetime, Tier]] = []
    for todo in projects.rglob("project_todo_*.md"):
        try:
            mtime = datetime.fromtimestamp(todo.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        age = now - mtime
        if age > timedelta(days=180):
            continue
        tier: Tier
        if age > timedelta(days=90):
            tier = "routine"
        elif age > timedelta(days=30):
            tier = "notable"
        elif age > timedelta(days=7):
            tier = "urgent"
        else:
            continue  # too fresh
        out.append((todo, mtime, tier))
    return out


_WIKILINK_RX = re.compile(r"\[\[([^\]\|#]+)(?:[\|#][^\]]*)?\]\]")


def _find_dead_links() -> list[tuple[Path, str]]:
    """Find `[[name]]` references that don't resolve to any .md file in the vault."""
    settings = get_settings()
    if not settings.vault_root.is_dir():
        return []

    # Index all md filenames (without .md) in vault
    known: set[str] = set()
    for md in settings.vault_root.rglob("*.md"):
        known.add(md.stem)

    dead: list[tuple[Path, str]] = []
    for md in settings.vault_root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ref in _WIKILINK_RX.findall(text):
            target = ref.strip()
            if target and target not in known:
                dead.append((md, target))
    return dead


def _render_markdown(digest: ReviewerDigest, day: date) -> str:
    def _aging(item: TodoAgeReport) -> str:
        return (
            f"- **{item.tier}** `{item.file_path}` "
            f"(last {item.last_modified}) — {item.suggestion}"
        )

    def _drift(item: DriftFinding) -> str:
        files = ", ".join(f"`{f}`" for f in item.files_involved)
        return f"- **{item.severity}** {item.description} ({files})"

    def _dead(item: DeadLink) -> str:
        return f"- `{item.source_file}` → `[[{item.reference}]]`"

    def _section(items: list[str]) -> str:
        return "\n".join(items) if items else "_(none)_"

    return (
        "---\n"
        f"date: {day.isoformat()}\n"
        "kind: reviewer-digest\n"
        "---\n\n"
        f"# Reviewer digest — {day.isoformat()}\n\n"
        f"## Summary\n\n{digest.summary}\n\n"
        "## TODOs aging past tier\n\n"
        f"{_section([_aging(t) for t in digest.aging_todos])}\n\n"
        "## Drift / contradictions\n\n"
        f"{_section([_drift(d) for d in digest.drift_findings])}\n\n"
        "## Dead links\n\n"
        f"{_section([_dead(d) for d in digest.dead_links])}\n"
    )


def _write_review(day: date, body: str) -> WriteResult:
    settings = get_settings()
    path = settings.vault_root / "reports" / f"reviewer-{day.isoformat()}.md"
    n = _write_atomic(path, body)
    return WriteResult(path=path, bytes_written=n)


def reviewer_node(state: FleetState) -> dict[str, Any]:
    """Produce the weekly review digest. Read-only-suggest."""
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()
    today = datetime.now(UTC).date()

    aging = _find_aging_todos()
    dead = _find_dead_links()

    aging_summary = "\n".join(
        f"- {p.relative_to(get_settings().vault_root)} (mtime {m.date().isoformat()}, age tier {t})"
        for p, m, t in aging[:50]
    ) or "(no aging TODOs)"

    dead_summary = "\n".join(
        f"- {p.relative_to(get_settings().vault_root)} → [[{ref}]]"
        for p, ref in dead[:30]
    ) or "(no dead links)"

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"Date: {today.isoformat()}\n\n"
                "Produce a ReviewerDigest. The aging-TODO + dead-link scans "
                "below are mechanical results — convert to structured findings "
                "with file-pointed suggestions. Look for drift in the activity "
                "logs you'd see at vault/agents/*/memory/.\n\n"
                "AGING TODOS (mechanical scan):\n"
                f"{aging_summary}\n\n"
                "DEAD LINKS (mechanical scan):\n"
                f"{dead_summary}\n"
            )
        ),
    ]

    digest: ReviewerDigest = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(digest, today)
    result = _write_review(today, markdown)

    return {
        "output": (
            f"reviewer digest: {result.path} "
            f"(aging={len(digest.aging_todos)}, drift={len(digest.drift_findings)}, "
            f"dead_links={len(digest.dead_links)})"
        ),
    }
