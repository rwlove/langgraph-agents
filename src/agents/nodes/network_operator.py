"""network-operator — Lovenet L1-L7 network operations.

Owns VLANs / ACLs / BGP / Omada / brain / DNS / certs / VPN egress and the way
Kubernetes workloads land on the right networks. Propose-first by default.

Prime directive: this agent **cannot break the network**. Class C+ side effects
route through `errand-runner` with a signed approval token; this node never
executes Omada / kubectl writes directly. The seven-clause execution gate
(read-back · failure-mode named · verbatim rollback · enumerated blast-radius ·
no recovery-path interaction · no bulk apply · positive verification) governs
what may even be *proposed* as Class C — anything failing the gate downgrades
to `action_class: A` (analysis only) or `handoff_target: user`.

Neighbor agent: `homelab-engineer`. Reject anything that's broad cluster /
Flux / storage work rather than L1-L7 network.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.state import ActionClass, AgentId, ApprovalRequest, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "network-operator"
_TEMPERATURE = 0.2

# Canonical write target for network-operator-proposed actions. Cluster-side
# network changes (CiliumNetworkPolicy, Gateway / HTTPRoute, NetworkPolicy,
# Service, DNSEndpoint) all flow through a single kubectl apply against
# home-ops. Off-cluster Omada writes (ACL rules, VLANs, AP config) need a
# separate omada-mcp.* target + its own errand-runner allowlist entry —
# tracked as a follow-up so the propose-then-execute pattern can be wired
# uniformly first. Mirrors smart_home_operator._HA_WRITE_TARGET.
_K8S_WRITE_TARGET = "kubectl-mcp.kubectl_apply"

# action_class values that imply a side effect requiring approval. Class A
# (read-only analysis) and B (vault-local commit) never need a user verdict;
# Class C (push/rollout) and D (apply directly) do.
_APPROVAL_REQUIRED_CLASSES: frozenset[ActionClass] = frozenset({"C", "D"})


class NetworkFinding(BaseModel):
    """Structured output for a network-operator pass.

    Mirrors the seven-clause execution gate from IDENTITY.md. `proposed_change`,
    `rollback`, `blast_radius`, and `recovery_path_touched` together capture
    enough for downstream review — `errand-runner` (or Rob) can act on the
    finding without re-deriving the safety analysis.
    """

    summary: str = Field(description="One-paragraph state of the network for this request.")
    failure_domain: str = Field(
        description=(
            "What loses connectivity if this change misbehaves. From the "
            "decision framework — name it explicitly, don't hand-wave."
        )
    )
    proposed_change: str = Field(
        description=(
            "The actual change to make. Single-object preferred. For Class C+ "
            "this must reference the current state read-back, not assumed state."
        )
    )
    blast_radius: str = Field(
        description=(
            "Enumerated affected hosts / VLANs / services. 'Probably nothing "
            "important' is not an enumeration."
        )
    )
    rollback: str = Field(
        description=(
            "Mechanical rollback. For Class C+ this is the pre-change config "
            "verbatim — Rob must be able to paste it back without further help."
        )
    )
    recovery_path_touched: bool = Field(
        default=False,
        description=(
            "True if the change touches brain WAN / brain LAN uplink / Omada "
            "controller uplink / wg-easy / brain firewalld OOB SSH pinhole "
            "(port 3231) / DNS resolvers / mgmt VLAN. If true, handoff "
            "defaults to user regardless of action_class."
        ),
    )
    action_class: ActionClass = Field(
        description=(
            "A=read-only, B=local commit / vault draft, C=push/rollout via "
            "errand-runner, D=apply directly. D should be exceedingly rare "
            "for this agent; most writes are C via errand-runner."
        )
    )
    handoff_target: Literal["user", "errand-runner", "homelab-engineer"] = "user"
    affected_resources: list[str] = Field(
        default_factory=list,
        description=(
            "VLAN IDs, ACL rule IDs, BGP neighbor IPs, AP names, SSIDs, DNS "
            "records, Omada object IDs, k8s Service/HTTPRoute/Gateway names."
        ),
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Vault paths, memory entries, omada object IDs, netbox IDs, kubectl outputs cited."
        ),
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
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(NetworkFinding)  # type: ignore[return-value]


def _render_markdown(finding: NetworkFinding, task_id: str) -> str:
    resources = "\n".join(f"- `{r}`" for r in finding.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in finding.references) or "_(none)_"
    warning = (
        "\n> ⚠️ Recovery path touched: this change affects brain / wg-easy / "
        "OOB pinhole / DNS / mgmt VLAN. Defaults to user handoff regardless "
        "of action_class.\n"
        if finding.recovery_path_touched
        else ""
    )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: network-finding\n"
        f"action_class: {finding.action_class}\n"
        f"handoff_target: {finding.handoff_target}\n"
        f"recovery_path_touched: {finding.recovery_path_touched}\n"
        "---\n\n"
        "# Network finding\n\n"
        f"{warning}"
        "## Summary\n\n"
        f"{finding.summary}\n\n"
        + (f"## Analysis\n\n{finding.analysis}\n\n" if finding.analysis else "")
        + "## Failure domain\n\n"
        f"{finding.failure_domain}\n\n"
        "## Proposed change\n\n"
        f"{finding.proposed_change}\n\n"
        "## Blast radius\n\n"
        f"{finding.blast_radius}\n\n"
        "## Rollback (verbatim)\n\n"
        f"```\n{finding.rollback}\n```\n\n"
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


def network_operator_node(state: FleetState) -> dict[str, Any]:
    """Analyze + propose for any Lovenet network request.

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
                "Produce a NetworkFinding. Prime directive: you cannot break "
                "the network. Run the seven-clause execution gate before "
                "choosing action_class C. If any clause fails, downgrade to "
                "A (analysis only) or set handoff_target=user with the gap "
                "named. Verbatim rollback. Enumerated blast radius. If the "
                "change touches the recovery path (brain / wg-easy / OOB "
                "pinhole / DNS / mgmt VLAN), set recovery_path_touched=true.\n\n"
                "SWEEP MODE: when the REQUEST is a weekly drift sweep "
                "(no specific change being proposed) and includes "
                "pre-fetched evidence, set action_class=A and put your "
                "substance in the `analysis` field — ranked findings "
                "from the evidence + one-sentence 'why it matters' + "
                "single recommended next step per finding. Leave "
                "proposed_change/blast_radius/rollback empty in sweep "
                "mode; those are for action proposals only."
            )
        ),
    ]

    finding: NetworkFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="network")

    update: dict[str, Any] = {
        "output": (
            f"network finding: {result.path} "
            f"(class={finding.action_class}, handoff={finding.handoff_target}, "
            f"recovery_path={finding.recovery_path_touched})"
        ),
    }

    # Approval composition. The schema's `handoff_target == "errand-runner"`
    # + `action_class in {C, D}` is the explicit signal the LLM emits for "I
    # want this network side effect executed, please get the user's verdict."
    # Recovery-path overrides (brain / wg-easy / OOB pinhole / DNS / mgmt
    # VLAN) force `user` handoff in the persona, so by the time we get here
    # those tasks have already been downgraded out of the errand-runner path.
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


def _compose_approval_request(finding: NetworkFinding) -> ApprovalRequest:
    """Translate a NetworkFinding into the ApprovalRequest errand-runner expects.

    Class C requires an undo path (errand-runner refuses Class C without one
    and escalates to D). For network changes we derive the undo by taking
    the finding's rollback text — verbatim — and prefixing it with the
    canonical write target. The rollback field is already mandatory +
    verbatim per the seven-clause execution gate, so we never end up here
    without one.

    payload_summary collapses the proposed change into the broker UI's
    single-line render. Truncated at 200 chars to keep Pushover / Zulip
    notification surfaces readable; the full draft lives in the vault and
    is linked from the broker message.
    """
    summary = finding.proposed_change.strip().splitlines()[0][:200]
    undo_path: str | None = None
    if finding.rollback.strip():
        # Embed the rollback as a description, not a callable target — the
        # broker UI displays it; errand-runner uses its presence (not its
        # content) as the Class-C gate.
        undo_path = f"{_K8S_WRITE_TARGET}: {finding.rollback.strip().splitlines()[0][:160]}"
    return ApprovalRequest(
        action_class=finding.action_class,
        target=_K8S_WRITE_TARGET,
        payload_summary=summary,
        undo_path=undo_path,
        proposed_by=_AGENT_ID,
    )
