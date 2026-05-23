"""artist — image generation via Pixelle-MCP / ComfyUI.

Composes a structured `GenerationRequest`: which ComfyUI workflow to invoke,
the diffusion prompt, params, expected output path. Errand-runner executes
the actual Pixelle-MCP call under signed approval.

The Pixelle MCP capability tuples in `tools/mcp.py` are stubs — populated
once Pixelle is deployed and the workflow inventory stabilizes.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm import llm
from agents.observability import get_logger
from agents.personas import load_persona
from agents.state import AgentId, FleetState

_AGENT_ID: AgentId = "artist"
_TEMPERATURE = 0.4  # creative prompt composition

slog = get_logger("nodes.artist")


def artist_node(state: FleetState) -> dict[str, Any]:
    """Compose a GenerationRequest from the user's image-gen ask."""
    persona = load_persona(_AGENT_ID)

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}\n\n"
                "Produce a GenerationRequest per your SOUL: which Pixelle workflow, "
                "the reworked diffusion prompt, params (width/height/steps/seed/cfg), "
                "expected vault output path, wall-time estimate, and a one-paragraph "
                "rationale.\n\n"
                "If the ask is for real-person portraits or NSFW content, refuse "
                "politely with the reason. If the workflow choice is ambiguous, ask "
                "ADMIN before generating."
            )
        ),
    ]

    response = llm(_AGENT_ID, temperature=_TEMPERATURE).invoke(messages)
    output = str(response.content).strip()
    slog.info("artist_request_composed", out_chars=len(output))

    return {"output": output}
