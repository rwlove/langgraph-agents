"""ml-tuner — model lifecycle decisions on the shared P40 (and future L40S/L4).

Quantitative voice: every recommendation has VRAM cost + precision/recall +
latency + $/inference. One knob at a time. Documents every model decision
for future-Rob to revisit.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ActionClass, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID = "ml-tuner"
_MODEL = "qwen2.5:14b"
_TEMPERATURE = 0.2

Knob = Literal[
    "model",
    "detection_threshold",
    "zone",
    "mask",
    "embedding_model",
    "keep_alive",
    "concurrent_load",
    "other",
]


class MLDecision(BaseModel):
    summary: str = Field(description="One-sentence what + why.")
    current_baseline: str = Field(description="Current metric + VRAM cost.")
    hypothesis: str = Field(description="What change is being proposed + expected delta.")
    knob: Knob
    knob_change: str = Field(description="From → to.")
    vram_delta_gib: float = Field(
        description="Change in resident VRAM. Negative = freeing. Zero if not a model swap."
    )
    measure_after: str = Field(
        description="How + when to measure outcome (e.g. '24h Frigate FP rate per camera')."
    )
    rollback: str = Field(description="How to revert if it makes things worse.")
    action_class: ActionClass
    affects_production: bool = Field(
        default=False,
        description="True if this changes detection behavior users can observe.",
    )


def _build_llm() -> BaseChatModel:
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(MLDecision)  # type: ignore[return-value]


def _render_markdown(d: MLDecision, task_id: str) -> str:
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: ml-decision\n"
        f"action_class: {d.action_class}\n"
        f"affects_production: {d.affects_production}\n"
        f"knob: {d.knob}\n"
        f"vram_delta_gib: {d.vram_delta_gib}\n"
        "---\n\n"
        f"# ML decision — {d.summary[:60]}\n\n"
        f"**Summary:** {d.summary}\n\n"
        f"**Knob:** {d.knob} — {d.knob_change}\n"
        f"**VRAM delta:** {d.vram_delta_gib:+.2f} GiB\n\n"
        "## Baseline\n\n"
        f"{d.current_baseline}\n\n"
        "## Hypothesis\n\n"
        f"{d.hypothesis}\n\n"
        "## Measurement plan\n\n"
        f"{d.measure_after}\n\n"
        "## Rollback\n\n"
        f"{d.rollback}\n"
    )


def ml_tuner_node(state: FleetState) -> dict[str, Any]:
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    triage_hint = ""
    if state.triage:
        triage_hint = f"\n\nTRIAGE CONTEXT:\n- summary: {state.triage.summary}\n"

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}{triage_hint}\n\n"
                "Produce an MLDecision. Quantitative — VRAM cost, expected "
                "precision/recall change, $/inference if relevant. Move ONE "
                "knob at a time. Define a measurement window before rollout."
            )
        ),
    ]

    decision: MLDecision = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(decision, state.task_id)
    result = write_draft(state.task_id, markdown, kind="ml")

    return {
        "output": (
            f"ml decision: {result.path} "
            f"(knob={decision.knob}, vram_delta={decision.vram_delta_gib:+.2f}GiB, "
            f"class={decision.action_class})"
        ),
    }
