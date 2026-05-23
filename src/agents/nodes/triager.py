"""Triager — the fleet's entry node.

Reads an inbox entry, classifies (domain/intent/mode), routes to one of 13
specialists. Uses qwen2.5:7b with structured-output to enforce a valid routing
target at JSON-parse time.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm import llm
from agents.personas import load_persona
from agents.state import AgentId, FleetState, TriageDecision

_TRIAGER_AGENT_ID: AgentId = "triager"
_TRIAGER_TEMPERATURE = 0.1


def _build_llm() -> Any:
    return llm(_TRIAGER_AGENT_ID, temperature=_TRIAGER_TEMPERATURE).with_structured_output(
        TriageDecision
    )


def triager_node(state: FleetState) -> dict[str, Any]:
    """Classify the inbox entry, return routing decision.

    Returns a partial-state dict that LangGraph merges into `state`.

    Caller-pinned routing: if the envelope (or any prior node) already
    set `state.target_agent`, respect it and skip the LLM call. Used
    by Windmill workflows that already know the right specialist
    (alertmanager-holmesgpt-notify routes by namespace; the Renovate
    triage workflow pins to homelab-engineer). This sidesteps the
    qwen2.5:7b triager's known mis-routing of triage-style prompts
    to errand-runner.
    """
    if state.target_agent is not None:
        # Synthesize a minimal TriageDecision so downstream nodes that
        # inspect state.triage keep working; the routing is already
        # decided.
        return {
            "triage": TriageDecision(
                summary=state.content[:120],
                domain="homelab",  # caller-pinned routes don't need accurate domain
                intent="action",
                target_agent=state.target_agent,
                confidence=1.0,
                reasoning="caller-pinned via envelope.target_agent — triager skipped",
            ),
            # leave target_agent as-is; we just confirm it
        }

    persona = load_persona(_TRIAGER_AGENT_ID)
    llm = _build_llm()

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                "INBOX ENTRY:\n\n"
                f"{state.content}\n\n"
                f"SOURCE: {state.source}\n"
                f"USER: {state.user}\n\n"
                "Produce the routing decision as structured JSON per the schema "
                "in your AGENTS.md."
            )
        ),
    ]

    decision: TriageDecision = llm.invoke(messages)

    return {
        "triage": decision,
        "target_agent": decision.target_agent,
    }
