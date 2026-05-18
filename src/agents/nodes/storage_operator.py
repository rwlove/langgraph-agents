"""storage-operator — Ceph + Longhorn + Garage + CNPG + Barman + NFS.

Owns the cluster storage hierarchy and the data durability decisions. Propose-
first by default. Prime directive: this agent **cannot lose data**. Class C+
side effects route through `errand-runner` with a signed approval token; this
node never executes PVC / OSD / Garage / CNPG writes directly. The eight-clause
execution gate (read-back · backup recency · failure mode named · verbatim
rollback · enumerated blast-radius · capacity verified · no recovery-path
interaction · positive verification) governs what may even be *proposed* as
Class C — anything failing the gate downgrades to `action_class: A` (analysis
only) or `handoff_target: user`.

Neighbor agent: `homelab-engineer`. Reject anything that's broad cluster /
Flux / non-storage work.
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

_AGENT_ID: AgentId = "storage-operator"
_TEMPERATURE = 0.2


class StorageFinding(BaseModel):
    """Structured output for a storage-operator pass.

    Mirrors the eight-clause execution gate from IDENTITY.md. `proposed_change`,
    `rollback`, `blast_radius`, `backup_recency`, and `recovery_path_touched`
    together capture enough for downstream review — `errand-runner` (or Rob)
    can act on the finding without re-deriving the safety analysis.
    """

    summary: str = Field(
        description="One-paragraph state of the storage situation for this request.",
    )
    failure_domain: str = Field(
        description=(
            "What data is at risk if this change misbehaves. From the "
            "decision framework — name it explicitly, don't hand-wave."
        )
    )
    proposed_change: str = Field(
        description=(
            "The actual change to make. Single-object preferred (one PVC, "
            "one bucket, one Longhorn Volume CR). For Class C+ this must "
            "reference the current state read-back, not assumed state."
        )
    )
    backup_recency: str = Field(
        description=(
            "For irreplaceable data: most recent backup verified successful "
            "within the acceptable window (Longhorn weekly <8d, CNPG Barman "
            "base+WAL continuous). For regenerable data: name the "
            "source-of-truth. CNPG/Barman CR reads are RBAC-denied — name "
            "the user-side check if you need one."
        )
    )
    blast_radius: str = Field(
        description=(
            "Enumerated workloads / PVCs / pools / buckets affected. "
            "'Probably nothing else uses it' is not an enumeration."
        )
    )
    capacity_check: str = Field(
        default="N/A",
        description=(
            "For footprint-increasing operations: free capacity in the "
            "target pool/backend, confirmed > 2x the requested size. N/A "
            "if the change doesn't grow footprint."
        ),
    )
    rollback: str = Field(
        description=(
            "Mechanical rollback. For Class C+ this is the pre-change spec "
            "verbatim — Rob must be able to paste it back without further "
            "help. If the rollback is 'restore from backup,' the gate is "
            "NOT satisfied — set action_class: A instead."
        )
    )
    recovery_path_touched: bool = Field(
        default=False,
        description=(
            "True if the change touches the Longhorn NFS backup target on "
            "beast, the Garage substrate on brain, beast slot-4 PCIe-affected "
            "OSDs/replicas, HA's CNPG cluster (recorder write path), or an "
            "in-flight Barman restore. If true, handoff defaults to user "
            "regardless of action_class."
        ),
    )
    action_class: ActionClass = Field(
        description=(
            "A=read-only, B=local commit / vault draft, C=push/rollout via "
            "errand-runner (single additive op), D=apply directly. D should "
            "be exceedingly rare; most writes are C via errand-runner."
        )
    )
    handoff_target: Literal[
        "user", "errand-runner", "homelab-engineer", "smart-home-operator", "ml-operator"
    ] = "user"
    affected_resources: list[str] = Field(
        default_factory=list,
        description=(
            "PVC names, PV names, Ceph pool names, Longhorn Volume CR names, "
            "Garage bucket names, CNPG cluster names, NFS export paths."
        ),
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Vault paths, memory entries, kubectl outputs cited, "
            "storage-class.instructions.md sections."
        ),
    )


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(StorageFinding)  # type: ignore[return-value]


def _render_markdown(finding: StorageFinding, task_id: str) -> str:
    resources = "\n".join(f"- `{r}`" for r in finding.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in finding.references) or "_(none)_"
    warning = (
        "\n> ⚠️ Recovery path touched: this change affects the Longhorn "
        "NFS backup target / Garage substrate / beast slot-4 OSDs / HA "
        "recorder / an in-flight Barman restore. Defaults to user "
        "handoff regardless of action_class.\n"
        if finding.recovery_path_touched
        else ""
    )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: storage-finding\n"
        f"action_class: {finding.action_class}\n"
        f"handoff_target: {finding.handoff_target}\n"
        f"recovery_path_touched: {finding.recovery_path_touched}\n"
        "---\n\n"
        "# Storage finding\n\n"
        f"{warning}"
        "## Summary\n\n"
        f"{finding.summary}\n\n"
        "## Failure domain\n\n"
        f"{finding.failure_domain}\n\n"
        "## Proposed change\n\n"
        f"{finding.proposed_change}\n\n"
        "## Backup recency\n\n"
        f"{finding.backup_recency}\n\n"
        "## Blast radius\n\n"
        f"{finding.blast_radius}\n\n"
        "## Capacity check\n\n"
        f"{finding.capacity_check}\n\n"
        "## Rollback (verbatim)\n\n"
        f"```\n{finding.rollback}\n```\n\n"
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


def storage_operator_node(state: FleetState) -> dict[str, Any]:
    """Analyze + propose for any storage request.

    Prime-directive enforcement is in the persona (SOUL.md / IDENTITY.md);
    the schema makes the gate fields mandatory so a downstream agent can
    machine-verify the safety analysis before acting.
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
                "Produce a StorageFinding. Prime directive: you cannot lose "
                "data. Run the eight-clause execution gate before choosing "
                "action_class C. If any clause fails, downgrade to A "
                "(analysis only) or set handoff_target=user with the gap "
                "named. For irreplaceable data, the most recent backup must "
                "be verified successful — name it in backup_recency. "
                "Verbatim rollback. Enumerated blast radius. If the change "
                "touches the Longhorn NFS backup target / Garage substrate "
                "/ beast slot-4 OSDs / HA recorder / in-flight Barman "
                "restore, set recovery_path_touched=true."
            )
        ),
    ]

    finding: StorageFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="storage")

    return {
        "output": (
            f"storage finding: {result.path} "
            f"(class={finding.action_class}, handoff={finding.handoff_target}, "
            f"recovery_path={finding.recovery_path_touched})"
        ),
    }
