"""smart-home-engineer — Home Assistant + Z-Wave + ESPHome + Frigate.

Repo-anchored to home-assistant-config. Diagnoses HA automations, drafts
config changes. HA writes go through errand-runner via ha-mcp; this agent
never writes state directly.
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

_AGENT_ID = "smart-home-engineer"
_MODEL = "qwen2.5:7b"
_TEMPERATURE = 0.2


class SmartHomeFinding(BaseModel):
    summary: str = Field(description="What's going on in HA / Z-Wave / ESPHome / Frigate.")
    entities: list[str] = Field(
        default_factory=list,
        description="HA entity IDs touched by the issue (e.g. light.porch).",
    )
    devices: list[str] = Field(
        default_factory=list,
        description="Z-Wave node IDs or ESPHome device names.",
    )
    diagnosis: str = Field(description="Hypothesis + how to confirm.")
    proposed_action: str = Field(description="Config change, ESPHome firmware bump, etc.")
    action_class: ActionClass
    handoff_target: Literal["user", "errand-runner", "homelab-engineer", "ml-tuner"] = "user"
    sleep_hours_warning: bool = Field(
        default=False,
        description="True if the change touches automation that could fire at 00:00-06:00.",
    )


def _build_llm() -> BaseChatModel:
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(SmartHomeFinding)  # type: ignore[return-value]


def _render_markdown(finding: SmartHomeFinding, task_id: str) -> str:
    entities = "\n".join(f"- `{e}`" for e in finding.entities) or "_(none)_"
    devices = "\n".join(f"- `{d}`" for d in finding.devices) or "_(none)_"
    warning = (
        "\n> ⚠️ Sleep-hours sensitivity: this change could fire between 00:00 and 06:00. "
        "Confirm with Rob before enabling.\n"
        if finding.sleep_hours_warning
        else ""
    )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: smart-home-finding\n"
        f"action_class: {finding.action_class}\n"
        f"handoff_target: {finding.handoff_target}\n"
        f"sleep_hours_warning: {finding.sleep_hours_warning}\n"
        "---\n\n"
        f"# Smart-home finding\n\n"
        f"{warning}"
        "## Summary\n\n"
        f"{finding.summary}\n\n"
        "## Entities\n\n"
        f"{entities}\n\n"
        "## Devices\n\n"
        f"{devices}\n\n"
        "## Diagnosis\n\n"
        f"{finding.diagnosis}\n\n"
        "## Proposed action\n\n"
        f"{finding.proposed_action}\n"
    )


def smart_home_engineer_node(state: FleetState) -> dict[str, Any]:
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
                "Produce a SmartHomeFinding. Reference HA entity IDs + "
                "Z-Wave node numbers when relevant. If the change touches "
                "automation that could fire 00:00-06:00, set "
                "sleep_hours_warning=true. HA writes hand off to errand-runner."
            )
        ),
    ]

    finding: SmartHomeFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="smart-home")

    return {
        "output": (
            f"smart-home finding: {result.path} "
            f"(class={finding.action_class}, entities={len(finding.entities)})"
        ),
    }
