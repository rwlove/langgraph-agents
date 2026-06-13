"""Typed schemas for the eval harness.

Everything that flows between the runner, judge, and report stages is a
Pydantic model so a malformed golden file or judge response fails at parse
time, not three stages later.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.state import AgentId, DataTier

# A single execution of an agent on one task, on one backend.
RunGroup = Literal["local", "claude"]

# Which output the judge preferred.
Preference = Literal["local", "claude", "tie"]

# Per-agent routing verdict the report emits.
#   offload-safe   — local is good enough; safe Path-2 offload target, no escalation needed.
#   route-to-api   — Claude clearly beats local AND the agent is Claude-eligible; pin to `claude`.
#   keep-local-fix — local is inadequate and Claude can't (or isn't allowed to) rescue it;
#                    the fix is local (prompt / evidence pre-fetch / model size), not escalation.
AgentLabel = Literal["offload-safe", "route-to-api", "keep-local-fix"]


class GoldenTask(BaseModel):
    """One representative task for an agent.

    `data_tier` defaults to `internal`. Tasks tagged `restricted` will never
    escalate at runtime (the emission gate blocks it), so the harness refuses
    to run a Claude comparison for them — keep golden sets to `public` /
    `internal` tasks (see ``evals/README.md``).
    """

    task_id: str
    content: str
    data_tier: DataTier = "internal"
    notes: str = ""
    tags: list[str] = Field(default_factory=list)


class GoldenSet(BaseModel):
    """A YAML-loaded set of golden tasks for one agent."""

    agent_id: AgentId
    tasks: list[GoldenTask]


class RunResult(BaseModel):
    """Output of one agent node run on one backend."""

    agent_id: AgentId
    task_id: str
    group: RunGroup
    output: str = ""
    # The agent's real deliverable, when it writes one. Most nodes write their
    # substance to a vault file (inbox/drafts/<kind>-<task_id>.md,
    # reports/research/<task_id>-*.md) and return only a short handle in
    # `output` — judging `output` alone scores the pointer, not the work. The
    # runner reads the file back into `draft` (and removes it) right after the
    # run. Empty when the node wrote no file.
    draft: str = ""
    latency_s: float = 0.0
    error: str | None = None
    # True when the Claude run was deliberately not attempted (ineligible agent).
    skipped: bool = False

    @property
    def candidate(self) -> str:
        """The text the judge should score: the real deliverable if the node
        wrote one, else the inline output handle."""
        return self.draft or self.output


class DimensionScores(BaseModel):
    """Per-dimension judge scores, 1 (poor) .. 5 (excellent)."""

    correctness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    safety_gate: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)

    @property
    def total(self) -> int:
        """Sum across dimensions, 4 .. 20."""
        return self.correctness + self.completeness + self.safety_gate + self.actionability


class JudgeVerdict(BaseModel):
    """A judged comparison (or single-output score) for one task.

    `claude_scores` is None when the agent is Claude-ineligible and only the
    local output was scored — `preference` is then `local` by construction.
    """

    agent_id: AgentId
    task_id: str
    preference: Preference
    local_scores: DimensionScores
    claude_scores: DimensionScores | None = None
    reasoning: str = ""
    error: str | None = None


class AgentReport(BaseModel):
    """Aggregated verdict + routing label for one agent."""

    agent_id: AgentId
    eligible: bool
    label: AgentLabel
    n_tasks: int
    n_judged: int
    claude_win_rate: float = 0.0
    mean_score_delta: float = 0.0
    mean_local_total: float = 0.0
    # Per-dimension means (1..5) keyed by dimension name. mean_local_dims is
    # the acceptability signal — read `correctness` + `safety_gate` here (the
    # dealbreaker dims) rather than the scalar delta. mean_claude_dims is over
    # paired tasks only (empty for Claude-ineligible agents).
    mean_local_dims: dict[str, float] = Field(default_factory=dict)
    mean_claude_dims: dict[str, float] = Field(default_factory=dict)
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
