"""Per-agent activity log writer.

Every node calls `log_activity()` after producing its output. The log files
live at `vault/agents/<agent_id>/memory/activity-log.md` and feed the
reporter's daily-digest aggregation.

Health-tracker has special rules per the privacy boundary: it logs only
metadata, never the medical content. Standard log_activity already only
records the summary the caller provides — so health-tracker just passes a
metadata-only summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agents.settings import get_settings
from agents.state import ActionClass, ActivityLogEntry, AgentId


def _agent_log_path(agent_id: AgentId) -> Path:
    return get_settings().vault_root / "agents" / agent_id / "memory" / "activity-log.md"


def log_activity(
    agent_id: AgentId,
    task_id: str,
    *,
    action_class: ActionClass,
    summary: str,
    outcome: str = "success",
    payload_hash: str | None = None,
) -> ActivityLogEntry:
    """Append a one-line activity entry. Creates dirs on demand."""
    entry = ActivityLogEntry(
        timestamp=datetime.now(UTC),
        agent=agent_id,
        task_id=task_id,
        action_class=action_class,
        summary=summary,
        outcome=outcome,  # type: ignore[arg-type]
        payload_hash=payload_hash,
    )
    path = _agent_log_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    line = (
        f"- `{entry.timestamp.isoformat()}` "
        f"**{entry.outcome}** "
        f"class-{entry.action_class} "
        f"task=`{entry.task_id}` "
        f"— {entry.summary}"
    )
    if entry.payload_hash:
        line += f" (payload {entry.payload_hash})"
    line += "\n"

    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return entry
