"""Graph state schema.

The state Pydantic model is the single source of truth for what flows between
nodes. Typed Literals on routing fields mean invalid agent IDs fail at
JSON-parse time, not at runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ---- Typed enums ----

AgentId = Literal[
    "triager",
    "reporter",
    "note-maker",
    "researcher",
    "coder",
    "errand-runner",
    "supervisor",
    "reviewer",
    "homelab-engineer",
    "network-operator",
    "storage-operator",
    "smart-home-operator",
    "ml-operator",
    "observability-operator",
    "health-tracker",
    "property-coordinator",
    "doc-writer",
]

ALL_AGENT_IDS: tuple[AgentId, ...] = (
    "triager",
    "reporter",
    "note-maker",
    "researcher",
    "coder",
    "errand-runner",
    "supervisor",
    "reviewer",
    "homelab-engineer",
    "network-operator",
    "storage-operator",
    "smart-home-operator",
    "ml-operator",
    "observability-operator",
    "health-tracker",
    "property-coordinator",
    "doc-writer",
)

Domain = Literal[
    "homelab",
    "smart-home",
    "property",
    "vehicles",
    "medical",
    "career",
    "hobby",
    "meta",
]

Intent = Literal[
    "question",
    "action",
    "research",
    "note",
    "bug",
    "idea",
    "reminder",
]

Mode = Literal["architect", "debugger", "optimizer", "default"]

ActionClass = Literal["A", "B", "C", "D"]

Source = Literal["voice", "zulip", "text", "holmesgpt", "test", "openwebui"]

TimeoutTier = Literal["30min", "4h", "7d"]


# ---- Sub-schemas ----


class TriageDecision(BaseModel):
    """Triager's structured output. Routing target is typed."""

    summary: str = Field(description="One-line restatement of the user's ask.")
    domain: Domain
    intent: Intent
    mode: Mode = "default"
    target_agent: AgentId = Field(
        description="Primary agent to route to. Must be one of the 13 valid IDs."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One paragraph max; why this routing.")
    fallback_agent: AgentId | None = None
    skill_hints: list[str] = Field(default_factory=list)
    requires_user_approval: bool = False


class RejectionSignal(BaseModel):
    """Specialist agents emit this to bounce a task back to supervisor."""

    rejected_by: AgentId
    reason: str
    suggested_target: AgentId
    context_to_preserve: str


class ApprovalRequest(BaseModel):
    """Payload an agent emits to request user approval before a Class C/D action."""

    action_class: ActionClass
    target: str = Field(description="What the action affects (service, file, URL).")
    payload_summary: str
    undo_path: str | None = None
    proposed_by: AgentId
    cost_estimate_usd: float = 0.0


class ActivityLogEntry(BaseModel):
    """Append-only audit entry written to vault/agents/<id>/memory/activity-log.md."""

    timestamp: datetime
    agent: AgentId
    task_id: str
    action_class: ActionClass
    summary: str
    outcome: Literal["success", "error", "escalated", "rejected", "in_progress"]
    payload_hash: str | None = None  # not the payload itself


# ---- Main graph state ----


class FleetState(BaseModel):
    """The shared state flowing through every node."""

    # --- task identity ---
    task_id: str
    source: Source
    content: str = Field(description="Raw inbox content (voice transcript, text, etc.)")
    user: str = "rob"

    # --- routing ---
    triage: TriageDecision | None = None
    target_agent: AgentId | None = None
    cascade_count: int = 0  # how many times we've re-routed; supervisor enforces limit=2
    rejection: RejectionSignal | None = None

    # --- approval flow ---
    approval_request: ApprovalRequest | None = None
    approval_granted: bool | None = None
    approval_token: str | None = None  # n8n-signed; verified by errand-runner

    # --- awaiting-user state machine ---
    awaiting_user_since: datetime | None = None
    timeout_tier: TimeoutTier | None = None

    # --- cost caps ---
    cost_cap_hit: bool = False
    accumulated_cost_usd: float = 0.0

    # --- output ---
    output: str | None = None
    activity_log_entries: list[ActivityLogEntry] = Field(default_factory=list)

    # --- meta ---
    started_at: datetime = Field(default_factory=_now_utc)
