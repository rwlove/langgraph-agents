"""Aggregate per-task judge verdicts into a per-agent routing label.

Label decision (see ``AgentLabel`` in schema for what each means):

    route-to-api    eligible AND Claude clearly wins (win-rate + score delta)
    offload-safe    otherwise, when local is adequate on its own
    keep-local-fix  local is inadequate and escalation won't (or can't) rescue it

Thresholds are deliberate, conservative starting points — tune them as real
golden-set data accrues. They are module constants so a tuning change is one
diff and shows up in `git blame`.
"""

from __future__ import annotations

from statistics import mean
from typing import TYPE_CHECKING

from evals.registry import is_claude_eligible
from evals.schema import AgentLabel, AgentReport, JudgeVerdict

if TYPE_CHECKING:
    from agents.state import AgentId

# Fraction of paired tasks where Claude must be preferred to justify routing up.
_CLAUDE_WIN_RATE = 0.6
# Mean (claude.total - local.total) on the 4..20 dimension-sum scale.
_CLAUDE_DELTA = 2.0
# Mean local total (of 20) at or above which local is "good enough" to offload to.
_LOCAL_ADEQUATE_TOTAL = 14.0

# The four scored dimensions (see rubrics/default.md). correctness + safety_gate
# are the dealbreakers — a low local score there is unacceptable for an ops
# agent regardless of the aggregate delta.
_DIMENSIONS = ("correctness", "completeness", "safety_gate", "actionability")


def _dim_means(verdicts: list[JudgeVerdict], attr: str) -> dict[str, float]:
    """Mean of each dimension (1..5) over `verdicts`, reading `attr`
    (``local_scores`` or ``claude_scores``). Empty if none carry it."""
    scored = [getattr(v, attr) for v in verdicts if getattr(v, attr) is not None]
    if not scored:
        return {}
    return {d: mean(getattr(s, d) for s in scored) for d in _DIMENSIONS}


def _label(
    *, eligible: bool, mean_local: float, win_rate: float, delta: float, has_pairs: bool
) -> AgentLabel:
    if eligible and has_pairs and win_rate >= _CLAUDE_WIN_RATE and delta >= _CLAUDE_DELTA:
        return "route-to-api"
    if mean_local >= _LOCAL_ADEQUATE_TOTAL:
        return "offload-safe"
    return "keep-local-fix"


def build_report(agent_id: AgentId, verdicts: list[JudgeVerdict]) -> AgentReport:
    """Roll up `verdicts` for one agent into a routing label."""
    eligible = is_claude_eligible(agent_id)
    judged = [v for v in verdicts if v.error is None]
    paired = [v for v in judged if v.claude_scores is not None]

    mean_local = mean([v.local_scores.total for v in judged]) if judged else 0.0
    win_rate = sum(1 for v in paired if v.preference == "claude") / len(paired) if paired else 0.0
    delta = (
        mean([v.claude_scores.total - v.local_scores.total for v in paired])  # type: ignore[union-attr]
        if paired
        else 0.0
    )

    label = _label(
        eligible=eligible,
        mean_local=mean_local,
        win_rate=win_rate,
        delta=delta,
        has_pairs=bool(paired),
    )
    return AgentReport(
        agent_id=agent_id,
        eligible=eligible,
        label=label,
        n_tasks=len(verdicts),
        n_judged=len(judged),
        claude_win_rate=win_rate,
        mean_score_delta=delta,
        mean_local_total=mean_local,
        mean_local_dims=_dim_means(judged, "local_scores"),
        mean_claude_dims=_dim_means(paired, "claude_scores"),
        verdicts=verdicts,
    )
