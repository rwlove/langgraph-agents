"""artist — image generation via artokun/comfyui-mcp.

Composes a structured `GenerationRequest`: which comfyui-mcp generation tool
to invoke (generate_image / generate_with_controlnet / generate_with_ip_adapter,
or enqueue_workflow for a full graph), the diffusion prompt, params, expected
output path. Errand-runner executes the actual comfyui-mcp call under signed
approval; the write tuples live in its allowlist, the read-only subset in
artist's.

Backend: ComfyUI on the DGX Spark (GB10) at comfyui-spark.ai.svc:8188, run
with --lowvram and a fresh basedir (models re-download on first use). The
GB10 is time-sliced and its unified memory is shared with ollama-spark, so
available VRAM is dynamic — query get_system_stats before sizing big jobs.
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
                "Produce a GenerationRequest per your SOUL: which comfyui-mcp "
                "generation tool (generate_image / generate_with_controlnet / "
                "generate_with_ip_adapter, or enqueue_workflow), the reworked "
                "diffusion prompt, params (width/height/steps/seed/cfg, model/"
                "checkpoint), expected vault output path, wall-time estimate, and "
                "a one-paragraph rationale.\n\n"
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
