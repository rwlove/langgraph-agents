"""security — surveillance + physical-security analyst.

Reads Frigate events + HA door/lock/motion entities; produces evidence-first
summaries with timestamps + clip refs. Doesn't take actions on its own;
proposals route to errand-runner for HA writes (lock door, arm scene, etc).

Frigate access is direct HTTP (no MCP wrapper) — see SOUL for the why.
For now this node only produces narrative findings; future enhancement
adds a structured event/clip schema + direct httpx calls to Frigate.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm import llm
from agents.observability import get_logger
from agents.personas import load_persona
from agents.state import AgentId, FleetState

_AGENT_ID: AgentId = "security"
_TEMPERATURE = 0.2  # factual evidence-summary work

slog = get_logger("nodes.security")


def security_node(state: FleetState) -> dict[str, Any]:
    """Produce a security finding from the user's question.

    For v1: persona + LLM. The node does NOT yet directly query Frigate;
    that integration lands as a follow-up (will require httpx + a Frigate
    auth token wired through settings).
    """
    persona = load_persona(_AGENT_ID)

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"QUESTION:\n\n{state.content}\n\n"
                "Produce a security finding per your SOUL. Distinguish observation "
                "from inference. Time-stamp everything. Surface anomalies first.\n\n"
                "Note: in v1 you don't yet have direct Frigate / HA access — if "
                "evidence-retrieval is required, propose what queries should run "
                "(GET /api/events?after=..., ha_get_state lock.front_door) and "
                "what you'd do with the results. The actual queries land in a "
                "follow-up that wires httpx + ha-mcp into your tool surface."
            )
        ),
    ]

    response = llm(_AGENT_ID, temperature=_TEMPERATURE).invoke(messages)
    output = str(response.content).strip()
    slog.info("security_finding_drafted", out_chars=len(output))

    return {"output": output}
