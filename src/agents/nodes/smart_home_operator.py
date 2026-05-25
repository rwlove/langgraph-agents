"""smart-home-operator — Home Assistant + protocol hubs + device fleet.

Owns HA core, all integration hubs (Z-Wave / Zigbee / Matter / ESPHome /
EMQX / Wyoming / Frigate / Music Assistant / Node-RED),
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
from agents.nodes._evidence import gather_evidence
from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ActionClass, AgentId, ApprovalRequest, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "smart-home-operator"
_TEMPERATURE = 0.2

# Canonical HA write target. The errand-runner allowlist accepts
# `ha-mcp.call_service` (see tools/mcp.py:ALLOWLISTS) and every HA side
# effect — light toggle, automation reload, integration restart — is shaped
# as a service call. Specialist-side we never invent a different method;
# undo paths embed the inverse call in their summary instead.
_HA_WRITE_TARGET = "ha-mcp.call_service"

# action_class values that imply a side effect requiring approval. Class A
# (read-only analysis) and B (vault-local commit) never need a user verdict;
# Class C (push/rollout) and D (apply directly) do.
_APPROVAL_REQUIRED_CLASSES: frozenset[ActionClass] = frozenset({"C", "D"})


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
        description=("Vault paths, memory entries, ha_*-tool outputs cited, ha-config repo paths."),
    )
    analysis: str = Field(
        default="",
        description=(
            "Free-form analysis the structured fields above don't capture. "
            "Use this for sweep-mode (weekly drift) where you're analyzing "
            "evidence without proposing a Class-C action: list each notable "
            "datum from the evidence block + a one-sentence 'why it matters', "
            "ranked by risk. The eight-clause gate still applies to "
            "proposed_change / blast_radius / rollback when this is an "
            "action proposal; for sweep mode they can be empty and this "
            "field carries the substance."
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
        + (f"## Analysis\n\n{finding.analysis}\n\n" if finding.analysis else "")
        + "## Failure domain\n\n"
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


def _load_intent_map() -> str:
    """Load the smart-home device intent map YAML for inclusion in the prompt.

    The intent map carries the SEMANTIC layer on top of HA's auto-discoverable
    data — HACS integration opinions, critical-device context, device-class
    severity. See `agents/workspaces/smart-home-operator/device-intent-map.yaml`.

    Falls back to empty string if the file doesn't exist or is empty. The
    agent still works — it just lacks the semantic context for critical /
    non-obvious devices.
    """
    settings = get_settings()
    path = settings.workspaces_dir / _AGENT_ID / "device-intent-map.yaml"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return text


async def smart_home_operator_node(state: FleetState) -> dict[str, Any]:
    """Analyze + propose for any HA / smart-home request.

    Prime-directive enforcement is in the persona; the schema makes the
    gate fields mandatory so a downstream agent can machine-verify the
    safety analysis before acting.
    """
    evidence = await gather_evidence(_AGENT_ID, state.content)
    evidence_block = (
        f"\n\nPRE-FETCHED EVIDENCE (from MCP tools):\n{evidence}"
        if evidence else ""
    )

    persona = load_persona(_AGENT_ID)
    model = _build_llm()

    triage_hint = ""
    if state.triage:
        triage_hint = (
            f"\n\nTRIAGE CONTEXT:\n- domain: {state.triage.domain}\n"
            f"- intent: {state.triage.intent}\n- mode: {state.triage.mode}\n"
            f"- summary: {state.triage.summary}\n"
        )

    intent_map = _load_intent_map()
    intent_context = ""
    if intent_map:
        intent_context = (
            "\n\nDEVICE INTENT MAP (hand-maintained semantic layer — refer "
            "to this for HACS integration opinions, critical-device meaning, "
            "and device-class severity. Treat as authoritative over generic "
            "HA defaults.):\n\n"
            f"```yaml\n{intent_map}\n```\n"
        )

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}{evidence_block}{triage_hint}{intent_context}\n\n"
                "Produce a SmartHomeFinding. Prime directive: you cannot "
                "break Home Assistant. Run the eight-clause execution gate "
                "before choosing action_class C. Reference HA entity IDs + "
                "Z-Wave node numbers when relevant. For YAML changes, "
                "ha_check_config result in config_validated. If the change "
                "touches a safety device / recorder / core integration "
                "disable, set recovery_path_touched=true. If the change "
                "could fire 00:00-06:00, set sleep_hours_warning=true. "
                "Verbatim rollback. Enumerated blast radius. HA writes "
                "hand off to errand-runner.\n\n"
                "SWEEP MODE: when the REQUEST is a weekly drift sweep "
                "(no specific YAML change being proposed) and includes "
                "pre-fetched evidence, set action_class=A and put your "
                "substance in the `analysis` field — ranked findings "
                "from the evidence + one-sentence 'why it matters' + "
                "single recommended next step per finding. Leave "
                "proposed_change/blast_radius/rollback empty in sweep "
                "mode; those are for action proposals only."
            )
        ),
    ]

    finding: SmartHomeFinding = model.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="smart-home")

    obsidian_link = result.obsidian_uri()
    update: dict[str, Any] = {
        "output": (
            f"[Open in Obsidian ↗]({obsidian_link})\n\n"
            "---\n\n"
            f"{markdown}"
        ),
    }

    # Approval composition. The schema's `handoff_target == "errand-runner"`
    # + `action_class in {C, D}` is the explicit signal the LLM emits for "I
    # want this HA side effect executed, please get the user's verdict."
    # Recovery-path / sleep-hours overrides force `user` handoff in the
    # persona, so by the time we get here those tasks have already been
    # downgraded out of the errand-runner path.
    if (
        finding.handoff_target == "errand-runner"
        and finding.action_class in _APPROVAL_REQUIRED_CLASSES
    ):
        update["approval_request"] = _compose_approval_request(finding)
        # Specialist → errand-runner routing. The fleet graph's
        # `_route_after_specialist` reads this; without it the graph would
        # END before the errand-runner ever sees the request.
        update["target_agent"] = "errand-runner"

    return update


def _compose_approval_request(finding: SmartHomeFinding) -> ApprovalRequest:
    """Translate a SmartHomeFinding into the ApprovalRequest errand-runner expects.

    Class C requires an undo path (errand-runner refuses Class C without one
    and escalates to D). For HA we derive the undo by taking the finding's
    rollback text — verbatim — and prefixing it with the canonical write
    target. The rollback field is already mandatory + verbatim per the
    eight-clause execution gate, so we never end up here without one.

    payload_summary collapses the proposed change into the broker UI's
    single-line render. Truncated at 200 chars to keep Pushover / Zulip
    notification surfaces readable; the full draft lives at result.path
    and is linked from the broker message.
    """
    summary = finding.proposed_change.strip().splitlines()[0][:200]
    undo_path: str | None = None
    if finding.rollback.strip():
        # Embed the rollback as a description, not a callable target — the
        # broker UI displays it; errand-runner uses its presence (not its
        # content) as the Class-C gate.
        undo_path = f"{_HA_WRITE_TARGET}: {finding.rollback.strip().splitlines()[0][:160]}"
    return ApprovalRequest(
        action_class=finding.action_class,
        target=_HA_WRITE_TARGET,
        payload_summary=summary,
        undo_path=undo_path,
        proposed_by=_AGENT_ID,
    )
