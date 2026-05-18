"""smart-home-operator — Home Assistant + protocol hubs + device fleet.

Owns HA core, all integration hubs (Z-Wave / Zigbee / Matter / ESPHome /
EMQX / Wyoming / Frigate / Music Assistant / Node-RED / HA-adjacent n8n),
the HA YAML repo at `~/workspace/claude-workspace/home-assistant-config/`,
and the HA CNPG connection wiring. Propose-first by default. Prime
directive: this agent **cannot break Home Assistant**. Class C+ side
effects route through `errand-runner` with a signed approval token; this
node never executes HA service calls / YAML reloads / integration toggles
directly. The eight-clause execution gate (read-back · failure mode named
· verbatim rollback · enumerated blast-radius · no safety-device
interaction · config validated · no bulk/cascading apply · positive
verification) governs what may even be *proposed* as Class C — anything
failing the gate downgrades to `action_class: A` (analysis only) or
`handoff_target: user`.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.state import ActionClass, AgentId, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "smart-home-operator"
_TEMPERATURE = 0.2


class SmartHomeFinding(BaseModel):
    """Structured output for a smart-home-operator pass.

    Mirrors the eight-clause execution gate from IDENTITY.md. The
    sleep_hours_warning + safety_device_touched flags are HA-specific
    overrides that force user handoff regardless of action_class.
    """

    summary: str = Field(description="What's going on in HA / Z-Wave / ESPHome / Frigate.")
    failure_domain: str = Field(
        description=(
            "What stops working in the house if this misbehaves. Name "
            "specific automations, devices, integrations — not 'something'."
        )
    )
    entities: list[str] = Field(
        default_factory=list,
        description="HA entity IDs touched by the issue (e.g. light.porch).",
    )
    devices: list[str] = Field(
        default_factory=list,
        description="Z-Wave node IDs, Zigbee device names, ESPHome device slugs.",
    )
    diagnosis: str = Field(description="Hypothesis + how to confirm.")
    proposed_change: str = Field(
        description=(
            "The actual change to make. Single-object preferred. For Class "
            "C+ this must reference the current state read-back."
        )
    )
    config_validated: str = Field(
        default="N/A",
        description=(
            "For YAML changes: `ha_check_config` result. For template "
            "changes: `ha_eval_template` result against current state. "
            "N/A if the change isn't config-shaped."
        ),
    )
    blast_radius: str = Field(
        description=(
            "Enumerated automations / dashboards / scripts / scenes / "
            "downstream integrations that reference the affected "
            "entity/area/helper/package. 'Probably nothing else uses it' "
            "is not an enumeration."
        )
    )
    rollback: str = Field(
        description=(
            "Mechanical rollback. For Class C+ this is the pre-change YAML "
            "/ automation payload / entity state verbatim — Rob must be "
            "able to paste it back without further help."
        )
    )
    recovery_path_touched: bool = Field(
        default=False,
        description=(
            "True if the change touches a safety device (door locks, "
            "garage doors, smoke/CO/leak detectors, alarm sensors, alarm "
            "arming state, thermostat setpoints during occupied hours, "
            "water shutoff, oven/range), the HA CNPG recorder, or any "
            "core integration disable. If true, handoff defaults to user."
        ),
    )
    sleep_hours_warning: bool = Field(
        default=False,
        description="True if this change could fire automation between 00:00-06:00.",
    )
    action_class: ActionClass = Field(
        description=(
            "A=read-only, B=local commit / vault draft, C=push/rollout via "
            "errand-runner (single helper/label/friendly-name/additive "
            "package), D=apply directly. D should be exceedingly rare; "
            "most writes are C via errand-runner."
        )
    )
    handoff_target: Literal[
        "user", "errand-runner", "homelab-engineer", "storage-operator", "ml-operator"
    ] = "user"
    affected_resources: list[str] = Field(
        default_factory=list,
        description=(
            "HA entity IDs, automation IDs, package paths, integration "
            "IDs, dashboard IDs, MQTT topics."
        ),
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Vault paths, memory entries, ha_*-tool outputs cited, "
            "ha-config repo paths."
        ),
    )


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(SmartHomeFinding)  # type: ignore[return-value]


def _render_markdown(finding: SmartHomeFinding, task_id: str) -> str:
    entities = "\n".join(f"- `{e}`" for e in finding.entities) or "_(none)_"
    devices = "\n".join(f"- `{d}`" for d in finding.devices) or "_(none)_"
    resources = "\n".join(f"- `{r}`" for r in finding.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in finding.references) or "_(none)_"
    warnings = ""
    if finding.recovery_path_touched:
        warnings += (
            "\n> ⚠️ Recovery path touched: this change affects a safety "
            "device, the HA recorder, or a core integration. Defaults to "
            "user handoff regardless of action_class.\n"
        )
    if finding.sleep_hours_warning:
        warnings += (
            "\n> 🌙 Sleep-hours sensitivity: this change could fire "
            "between 00:00 and 06:00. Confirm guard conditions before "
            "enabling.\n"
        )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: smart-home-finding\n"
        f"action_class: {finding.action_class}\n"
        f"handoff_target: {finding.handoff_target}\n"
        f"recovery_path_touched: {finding.recovery_path_touched}\n"
        f"sleep_hours_warning: {finding.sleep_hours_warning}\n"
        "---\n\n"
        "# Smart-home finding\n\n"
        f"{warnings}"
        "## Summary\n\n"
        f"{finding.summary}\n\n"
        "## Failure domain\n\n"
        f"{finding.failure_domain}\n\n"
        "## Entities\n\n"
        f"{entities}\n\n"
        "## Devices\n\n"
        f"{devices}\n\n"
        "## Diagnosis\n\n"
        f"{finding.diagnosis}\n\n"
        "## Proposed change\n\n"
        f"{finding.proposed_change}\n\n"
        "## Config validated\n\n"
        f"{finding.config_validated}\n\n"
        "## Blast radius\n\n"
        f"{finding.blast_radius}\n\n"
        "## Rollback (verbatim)\n\n"
        f"```\n{finding.rollback}\n```\n\n"
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


def smart_home_operator_node(state: FleetState) -> dict[str, Any]:
    """Analyze + propose for any HA / smart-home request.

    Prime-directive enforcement is in the persona; the schema makes the
    gate fields mandatory so a downstream agent can machine-verify the
    safety analysis before acting.
    """
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    triage_hint = ""
    if state.triage:
        triage_hint = (
            f"\n\nTRIAGE CONTEXT:\n- domain: {state.triage.domain}\n"
            f"- intent: {state.triage.intent}\n- mode: {state.triage.mode}\n"
            f"- summary: {state.triage.summary}\n"
        )

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}{triage_hint}\n\n"
                "Produce a SmartHomeFinding. Prime directive: you cannot "
                "break Home Assistant. Run the eight-clause execution gate "
                "before choosing action_class C. Reference HA entity IDs + "
                "Z-Wave node numbers when relevant. For YAML changes, "
                "ha_check_config result in config_validated. If the change "
                "touches a safety device / recorder / core integration "
                "disable, set recovery_path_touched=true. If the change "
                "could fire 00:00-06:00, set sleep_hours_warning=true. "
                "Verbatim rollback. Enumerated blast radius. HA writes "
                "hand off to errand-runner."
            )
        ),
    ]

    finding: SmartHomeFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="smart-home")

    return {
        "output": (
            f"smart-home finding: {result.path} "
            f"(class={finding.action_class}, handoff={finding.handoff_target}, "
            f"recovery_path={finding.recovery_path_touched}, "
            f"sleep_hours={finding.sleep_hours_warning})"
        ),
    }
