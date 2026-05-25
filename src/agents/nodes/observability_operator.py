"""observability-operator — Prometheus rules + AlertManager + Loki + Grafana + HolmesGPT prompts.

Owns alert authoring, scrape configs, dashboard structure, log retention, and
AI-triage prompt tuning. Propose-first by default. Prime directive: this agent
**cannot bury a real alert under flap**. Class C+ side effects route through
`errand-runner` with a signed approval token. The eight-clause execution gate
(read-back · flap-tested · failure mode named in both directions · verbatim
rollback · enumerated blast-radius · no silent muting · routing verified ·
positive verification) governs what may be proposed as Class C.

Calibrated voice: every alert proposal names both the **flood** mode (noise
crowds real signal) and the **mute** mode (real signal silenced). Without
that framing, the proposal is incomplete.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.nodes._evidence import gather_evidence
from agents.personas import load_persona
from agents.state import ActionClass, AgentId, ApprovalRequest, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "observability-operator"
_TEMPERATURE = 0.2

# Canonical write target for observability-operator-proposed actions.
# PrometheusRule, ServiceMonitor, PodMonitor, AlertmanagerConfig, Silence,
# Grafana dashboard ConfigMap — every observability side effect this agent
# proposes lands as a kubectl apply against home-ops. Grafana-mcp UI / API
# writes aren't part of this path (no admin-side gMCP capability yet).
# Mirrors smart_home_operator._HA_WRITE_TARGET.
_K8S_WRITE_TARGET = "kubectl-mcp.kubectl_apply"

# action_class values that imply a side effect requiring approval. Class A
# (read-only analysis) and B (vault-local commit) never need a user verdict;
# Class C (push/rollout) and D (apply directly) do.
_APPROVAL_REQUIRED_CLASSES: frozenset[ActionClass] = frozenset({"C", "D"})

AlertVolumeDelta = Literal["more", "fewer", "same", "n/a"]
RoutingTarget = Literal["pushover", "zulip", "holmesgpt", "n/a"]


class ObservabilityFinding(BaseModel):
    """Structured output for an observability-operator pass.

    Mirrors the eight-clause execution gate. Distinct from other operator
    findings: the `flood_mode` and `mute_mode` fields force the LLM to name
    BOTH failure modes — that's the discipline this role exists to enforce.
    """

    summary: str = Field(description="One-paragraph what + why.")
    flood_mode: str = Field(
        description=(
            "The flood failure mode: if the proposed change misbehaves toward "
            "noise, what notification surface gets crowded? Pushover spam? "
            "Zulip channel saturation? HolmesGPT triage backlog?"
        )
    )
    mute_mode: str = Field(
        description=(
            "The mute failure mode: if the proposed change misbehaves toward "
            "silence, what real alert gets buried? Existing rule shadowed? "
            "Receiver disabled? Severity bumped past existing thresholds?"
        )
    )
    proposed_change: str = Field(
        description=(
            "The actual change to make. Single-object preferred (one "
            "PrometheusRule, one ServiceMonitor, one silence, one panel)."
        )
    )
    has_for_clause: bool = Field(
        default=False,
        description=(
            "True if the proposed PrometheusRule has a `for:` clause of >= 5m "
            "for flap protection. False if the metric is genuinely non-transient "
            "(state-machine signal, fault flag) — in which case explain why "
            "no `for:` is needed in proposed_change."
        ),
    )
    routing_target: RoutingTarget = Field(
        default="n/a",
        description=(
            "Which AlertManager receiver this fires to. pushover = wake-the-human, "
            "zulip = visible-but-not-paging, holmesgpt = AI triage first then "
            "decide. n/a if the change isn't a routed alert."
        ),
    )
    alert_volume_delta: AlertVolumeDelta = Field(
        default="n/a",
        description=(
            "Direction of change in alert volume. 'more' = new alert(s) firing. "
            "'fewer' = silencing or threshold raise. 'same' = scope-preserving "
            "edit. 'n/a' = not an alert change (dashboard, scrape config)."
        ),
    )
    blast_radius: str = Field(
        description=(
            "Enumerated dashboards / runbooks / rules / downstream consumers "
            "(HolmesGPT prompts, Windmill workflows) that reference this rule / "
            "metric / dashboard. 'Probably nothing else uses it' is not an "
            "enumeration."
        )
    )
    rollback: str = Field(
        description=(
            "Mechanical rollback. Pre-change YAML verbatim. If the rollback "
            "is 'restore AlertManager config snapshot' (not a single rule), "
            "the gate is NOT satisfied — set action_class: A instead."
        )
    )
    recovery_path_touched: bool = Field(
        default=False,
        description=(
            "True if the change disables a receiver / silences an alert class "
            "/ raises a severity threshold past existing rules / reduces "
            "retention / removes a rule without a documented successor. If "
            "true, handoff defaults to user regardless of action_class."
        ),
    )
    positive_verification: str = Field(
        description=(
            "How to confirm the change works as intended. Did the rule fire "
            "at the expected condition? Did it silence at recovery? Did the "
            "routing land at the right receiver? Not 'PrometheusRule reconciled.'"
        )
    )
    action_class: ActionClass = Field(
        description=(
            "A=read-only, B=local commit / vault draft, C=push/rollout via "
            "errand-runner (single additive op), D=apply directly. D should "
            "be exceedingly rare for this agent."
        )
    )
    handoff_target: Literal[
        "user",
        "errand-runner",
        "homelab-engineer",
        "ml-operator",
        "storage-operator",
        "smart-home-operator",
        "network-operator",
    ] = "user"
    affected_resources: list[str] = Field(
        default_factory=list,
        description=(
            "PrometheusRule names, ServiceMonitor names, AlertmanagerConfig "
            "names, Grafana dashboard UIDs, Loki tenants, HolmesGPT prompt IDs."
        ),
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Vault paths, memory entries, prom queries cited, grafana panel "
            "URLs, AlertManager docs."
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
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(ObservabilityFinding)  # type: ignore[return-value]


def _render_markdown(f: ObservabilityFinding, task_id: str) -> str:
    resources = "\n".join(f"- `{r}`" for r in f.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in f.references) or "_(none)_"
    warning = (
        "\n> ⚠️ Recovery path touched: this change disables a receiver / "
        "silences an alert class / raises a severity threshold / reduces "
        "retention / removes a rule without a successor. Defaults to user "
        "handoff regardless of action_class.\n"
        if f.recovery_path_touched
        else ""
    )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: observability-finding\n"
        f"action_class: {f.action_class}\n"
        f"handoff_target: {f.handoff_target}\n"
        f"recovery_path_touched: {f.recovery_path_touched}\n"
        f"has_for_clause: {f.has_for_clause}\n"
        f"routing_target: {f.routing_target}\n"
        f"alert_volume_delta: {f.alert_volume_delta}\n"
        "---\n\n"
        "# Observability finding\n\n"
        f"{warning}"
        "## Summary\n\n"
        f"{f.summary}\n\n"
        + (f"## Analysis\n\n{f.analysis}\n\n" if f.analysis else "")
        + "## Flood mode (if this misbehaves toward noise)\n\n"
        f"{f.flood_mode}\n\n"
        "## Mute mode (if this misbehaves toward silence)\n\n"
        f"{f.mute_mode}\n\n"
        "## Proposed change\n\n"
        f"{f.proposed_change}\n\n"
        f"**Routing target:** `{f.routing_target}` · "
        f"**`for:` clause present:** {f.has_for_clause} · "
        f"**Alert volume delta:** {f.alert_volume_delta}\n\n"
        "## Blast radius\n\n"
        f"{f.blast_radius}\n\n"
        "## Positive verification\n\n"
        f"{f.positive_verification}\n\n"
        "## Rollback (verbatim)\n\n"
        f"```\n{f.rollback}\n```\n\n"
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


async def observability_operator_node(state: FleetState) -> dict[str, Any]:
    """Analyze + propose for any observability request.

    Prime-directive enforcement is in the persona; the schema makes the
    gate fields mandatory — in particular `flood_mode` and `mute_mode`
    force the LLM to name BOTH failure directions, which is the discipline
    this role exists to enforce.
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

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}{evidence_block}{triage_hint}\n\n"
                "Produce an ObservabilityFinding. Prime directive: you cannot "
                "bury a real alert under flap. Name BOTH failure modes — "
                "flood_mode (noise crowds real signal) AND mute_mode (real "
                "signal silenced). For any PrometheusRule proposal, has_for_clause "
                "MUST be true unless the metric is genuinely non-transient "
                "(explain why in proposed_change). If the change disables a "
                "receiver / silences an alert class / raises severity threshold / "
                "reduces retention / removes a rule without successor, set "
                "recovery_path_touched=true and action_class=A. Verbatim "
                "rollback. Enumerated blast radius.\n\n"
                "SWEEP MODE: when the REQUEST is a weekly drift sweep "
                "(no specific rule/route being proposed) and includes "
                "pre-fetched evidence, set action_class=A and put your "
                "substance in the `analysis` field — ranked findings "
                "from the evidence + one-sentence 'why it matters' + "
                "single recommended next step per finding. Leave "
                "proposed_change/blast_radius/rollback empty in sweep "
                "mode; those are for action proposals only."
            )
        ),
    ]

    finding: ObservabilityFinding = model.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="observability")

    update: dict[str, Any] = {
        "output": (
            f"observability finding: {result.path} "
            f"(class={finding.action_class}, handoff={finding.handoff_target}, "
            f"recovery_path={finding.recovery_path_touched}, "
            f"routing={finding.routing_target}, "
            f"for_clause={finding.has_for_clause})"
        ),
    }

    # Approval composition. The schema's `handoff_target == "errand-runner"`
    # + `action_class in {C, D}` is the explicit signal the LLM emits for "I
    # want this observability side effect executed, please get the user's
    # verdict." Recovery-path overrides (disabled receiver / silenced alert
    # class / raised severity / reduced retention / rule removed without a
    # successor) force `user` handoff in the persona, so by the time we get
    # here those tasks have already been downgraded out of the errand-runner
    # path.
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


def _compose_approval_request(finding: ObservabilityFinding) -> ApprovalRequest:
    """Translate an ObservabilityFinding into the ApprovalRequest errand-runner expects.

    Class C requires an undo path (errand-runner refuses Class C without one
    and escalates to D). For observability changes we derive the undo by
    taking the finding's rollback text — verbatim — and prefixing it with
    the canonical write target. The rollback field is already mandatory +
    verbatim per the eight-clause execution gate; if a rollback is "restore
    AlertManager config snapshot" (not a single rule) the gate flips
    action_class to A in the persona, so we never end up here without a
    paste-and-restart rollback.

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
