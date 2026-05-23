"""Graph state schema.

The state Pydantic model is the single source of truth for what flows between
nodes. Typed Literals on routing fields mean invalid agent IDs fail at
JSON-parse time, not at runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from operator import add
from typing import Annotated, Literal, cast, get_args

from pydantic import BaseModel, Field


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ---- Typed enums ----

AgentId = Literal[
    "triager",
    "historian",
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
    "reporter",
]

# Derived from the AgentId Literal — `get_args` returns the Literal members at
# import time, making AgentId the single source of truth for the agent list.
# Adding a new agent now only requires touching AgentId above; this tuple
# (and the NODES dict in agents.nodes.__init__) update automatically /
# raise in tests if you forget the node-function side.
ALL_AGENT_IDS: tuple[AgentId, ...] = cast("tuple[AgentId, ...]", get_args(AgentId))

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

Source = Literal["voice", "zulip", "text", "holmesgpt", "test", "openwebui", "cli"]

TimeoutTier = Literal["30min", "4h", "7d"]

# --- Task envelope enums (HOMELAB-SPEC Layer 5) -----------------------------
#
# `Origin` is the spec's `origin` field, distinct from the existing `Source`.
# Old callers send `source`; new callers may additionally send `origin`. The
# two will be unified when the substrate lands (Phase 4); until then both
# coexist and `origin` is optional.
Origin = Literal[
    "open-webui",
    "ha-voice",
    "android",
    "observer",
    "scheduled",
    "manual",
]

Requester = Literal["rob", "renee", "system"]

Priority = Literal["low", "normal", "high", "urgent"]

DataTier = Literal["public", "internal", "restricted"]


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


class RetryPolicy(BaseModel):
    """Retry policy carried on the task envelope (HOMELAB-SPEC Layer 5).

    Honored by the queue substrate when it lands (Phase 4). Until then this
    is informational — `/inbox` does not retry on its own, the caller does.
    """

    max_attempts: int = Field(default=1, ge=1)
    backoff_seconds: int = Field(default=0, ge=0)


# ---- Main graph state ----


class FleetState(BaseModel):
    """The shared state flowing through every node."""

    # --- task identity ---
    task_id: str
    source: Source
    content: str = Field(description="Raw inbox content (voice transcript, text, etc.)")
    user: str = "rob"

    # --- task envelope (HOMELAB-SPEC Layer 5; additive, all optional) ---
    #
    # These propagate from `InboxRequest` so they're visible to every node.
    # Behavior wiring lands in Phase 3 (idempotency, redaction, allowlist,
    # trace_id propagation); this addition is schema-only.
    trace_id: str | None = None
    origin: Origin | None = None
    requester: Requester | None = None
    intent_envelope: Intent | None = None
    priority: Priority = "normal"
    destructive: bool | None = None
    idempotency_key: str | None = None
    ttl_seconds: int | None = None
    retry_policy: RetryPolicy | None = None
    data_tier: DataTier = "internal"

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
    # Append-only across all nodes in one task run. Without the `add` reducer,
    # LangGraph treats list fields as replace, so a triager → specialist →
    # supervisor cascade would silently lose earlier audit entries when the
    # supervisor node returns an updated log.
    activity_log_entries: Annotated[list[ActivityLogEntry], add] = Field(default_factory=list)

    # --- meta ---
    started_at: datetime = Field(default_factory=_now_utc)
