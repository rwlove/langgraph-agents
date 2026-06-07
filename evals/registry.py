"""Claude-eligibility for the eval harness.

Eligibility here is the **categorical hard pin only** — agents that can never
use the Claude API regardless of the task. Today that is exactly
`health-tracker` (health data stays local; enforced three times over in
`agents.llm.llm`, `agents.router.score_route`, and the `assert_emission_allowed`
emission gate).

Restricted-tier eligibility is **per task, not per agent**:
`assert_emission_allowed` fails closed on `data_tier == "restricted"` for every
agent. So an agent that mostly handles restricted data is technically eligible
here but will not escalate on those tasks — evaluate it on its non-restricted
tasks instead (see ``evals/README.md``).
"""

from __future__ import annotations

from agents.state import AgentId

# Mirrors the hard pin in agents.llm.llm (health-tracker branch). If that pin
# ever grows, add the agent here too — the report uses this to decide whether a
# poor-on-Spark agent can be fixed by escalation (route-to-api) or only locally
# (keep-local-fix).
CLAUDE_INELIGIBLE: frozenset[AgentId] = frozenset({"health-tracker"})


def is_claude_eligible(agent_id: AgentId) -> bool:
    """True if `agent_id` may be served by the Claude API at all."""
    return agent_id not in CLAUDE_INELIGIBLE
