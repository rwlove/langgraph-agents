"""Deterministic router scorer — the local-vs-Claude (escalate) gate.

HOMELAB-SPEC Layer 6: the router "picks the cheapest model that can complete
the step," cheap/local/fast, with the explicit goal of reducing remote usage
over time. This module is the heuristic, enforce-now-conservative
implementation: plain rules over task signals, no local-model scorer.

It is intentionally a *pure* function. `score_route` reads the same structlog
contextvars `agents.llm` already reads (`data_tier`, plus the `est_input_tokens`
bound at ingress) and returns a decision; it constructs no models and performs
no I/O. `agents.llm.llm()` calls it at the single routing chokepoint, emits the
decision metric, and lets the existing escalate machinery (cost caps, the
restricted-tier emission gate) act on the result.

By default it escalates on exactly one capability-driven condition: estimated
input size exceeds a configured local-context ceiling — the only case where
local genuinely *cannot* do the work (Layer 6 "context length needed" + Layer 5
"route to a stronger model"). Everything else stays local.

Two further triggers — `destructive` (Layer 5 "destructive steps get the
strongest reviewer") and `cascade` (escalate after N supervisor re-routes) —
ship wired but DISABLED by default (`router_escalate_on_destructive` /
`router_escalate_on_cascade`). They reverse the enforce-now-conservative
posture and are gated on ground truth that doesn't exist yet (the fleet routes
100% local; there's no escalation-rate or failure-provenance signal to tune
them against). They stay off until that data exists. cascade in particular is a
weak signal — re-route count is not a local-failure count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agents.llm import ModelGroup
    from agents.settings import Settings
    from agents.state import AgentId

# Token estimation. We have no tokenizer dependency and don't want one on the
# hot path, so approximate: English prose averages ~4 chars/token, and the
# assembled prompt is always larger than the raw inbox `content` (system
# prompt + gathered evidence + activity log). The fixed overhead makes the
# estimate *over*-count, which is the safe direction — it can only push a task
# toward escalation, never hide a genuine overflow.
_CHARS_PER_TOKEN = 4
_PROMPT_OVERHEAD_TOKENS = 2000

# Source value for work offloaded from a Claude-Code session ("Path 2"). Tasks
# with this source never escalate to the metered Claude API — see
# `is_claude_code_source` and `settings.router_suppress_escalation_for_claude_code`.
_CLAUDE_CODE_SOURCE = "claude-code"


def is_claude_code_source(settings: Settings) -> bool:
    """True if the current task is a Claude-Code offload AND the Path-2 guard is on.

    Reads `source` from structlog contextvars (bound at /inbox and re-bound by
    the queue worker). `agents.llm.llm()` uses this to suppress an *explicit*
    ``escalate=True`` — the case `score_route` never sees because that arg is
    passed straight to the factory, not derived from the scorer.
    """
    if not settings.router_suppress_escalation_for_claude_code:
        return False
    return structlog.contextvars.get_contextvars().get("source") == _CLAUDE_CODE_SOURCE


def estimate_input_tokens(content: str) -> int:
    """Cheap, deterministic upper-ish estimate of assembled prompt size.

    Bound at ingress into the `est_input_tokens` contextvar so `score_route`
    can read it without re-deriving from raw content (which it doesn't have).
    """
    return len(content) // _CHARS_PER_TOKEN + _PROMPT_OVERHEAD_TOKENS


@dataclass(frozen=True)
class RouteDecision:
    """Result of the scorer. `reason` doubles as the metric label."""

    escalate: bool
    # local_default | context_overflow | restricted_pinned_local | scorer_disabled
    #   | destructive_escalation | cascade_escalation | claude_code_origin
    reason: str


def score_route(  # noqa: PLR0911 — returns map 1:1 to documented decision branches
    agent_id: AgentId, group: ModelGroup, settings: Settings
) -> RouteDecision:
    """Decide whether `agent_id`'s call should escalate to Claude.

    Conservative by construction — returns ``escalate=False`` for everything
    except a genuine local-context overflow. Never escalates restricted-tier
    data (belt-and-suspenders ahead of the hard `_build_claude` emission gate),
    and is a no-op when the call is already on the Claude group.
    """
    if not settings.router_scorer_enabled:
        return RouteDecision(escalate=False, reason="scorer_disabled")

    # Already heading to Claude (explicit override / escalate kwarg). The
    # scorer only ever flips local -> claude, never the reverse.
    if group == "claude":
        return RouteDecision(escalate=False, reason="local_default")

    ctx = structlog.contextvars.get_contextvars()

    # Restricted-tier data never leaves for a remote model. `_build_claude`
    # enforces this hard via assert_emission_allowed; the scorer refuses to
    # even decide to escalate it.
    if ctx.get("data_tier") == "restricted":
        return RouteDecision(escalate=False, reason="restricted_pinned_local")

    # Path-2 guard: work offloaded from a Claude-Code session never escalates to
    # the metered API — the caller is already a flat-rate Claude that can do the
    # work itself, so bounce back to it. Pinned local like restricted-tier, and
    # ahead of the capability triggers so even an overflowing Claude-Code task
    # stays local (and reports this reason, not context_overflow).
    cc_guard = settings.router_suppress_escalation_for_claude_code
    if cc_guard and ctx.get("source") == _CLAUDE_CODE_SOURCE:
        return RouteDecision(escalate=False, reason="claude_code_origin")

    # The one capability-driven trigger. Unbound (directly-enqueued tasks,
    # tests, legacy callers that never hit /inbox) reads as 0 -> stays local.
    # The bar is per-group: the P40's KV cache is smaller than the Spark's, so
    # a prompt that overflows the P40 (and must escalate) still fits the Spark.
    # `group == "claude"` already returned above, so group is one of the three
    # local groups here.
    est_input_tokens = ctx.get("est_input_tokens", 0)
    threshold = (
        settings.router_escalate_token_threshold_p40
        if group == "local-p40"
        else settings.router_escalate_token_threshold_spark
    )
    if est_input_tokens > threshold:
        return RouteDecision(escalate=True, reason="context_overflow")

    # Opt-in triggers (both default OFF). Checked after context_overflow so a
    # genuinely-overflowing destructive/cascading task reports the stronger
    # capability reason. Inert until the corresponding flag is flipped per
    # deployment; restricted-tier data was already pinned local above.
    if settings.router_escalate_on_destructive and ctx.get("destructive") is True:
        return RouteDecision(escalate=True, reason="destructive_escalation")

    if settings.router_escalate_on_cascade:
        cascade_count = ctx.get("cascade_count", 0)
        if isinstance(cascade_count, int) and cascade_count >= settings.router_cascade_threshold:
            return RouteDecision(escalate=True, reason="cascade_escalation")

    return RouteDecision(escalate=False, reason="local_default")
