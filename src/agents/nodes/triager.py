"""Triager — the fleet's entry node.

Reads an inbox entry, classifies (domain/intent/mode), routes to one of 13
specialists. Uses qwen2.5:7b with structured-output to enforce a valid routing
target at JSON-parse time.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import FleetState, TriageDecision

_TRIAGER_AGENT_ID = "triager"
_TRIAGER_MODEL = "qwen2.5:7b"
_TRIAGER_TEMPERATURE = 0.1


def _build_llm() -> Any:
    settings = get_settings()
    return ChatOllama(
        model=_TRIAGER_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TRIAGER_TEMPERATURE,
    ).with_structured_output(TriageDecision)


def triager_node(state: FleetState) -> dict[str, Any]:
    """Classify the inbox entry, return routing decision.

    Returns a partial-state dict that LangGraph merges into `state`.
    """
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

    decision: TriageDecision = llm.invoke(messages)  # type: ignore[assignment]

    return {
        "triage": decision,
        "target_agent": decision.target_agent,
    }
