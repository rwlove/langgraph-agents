"""homelab-engineer — Kubernetes / GitOps / homelab operations.

Repo-anchored to home-ops. Stability bias (per the persona). Uses kubectl-mcp
read-only for diagnostics + prometheus-mcp / grafana-mcp for cluster health.
Class C+ side effects route through errand-runner with a signed approval.
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

_AGENT_ID: AgentId = "homelab-engineer"
_TEMPERATURE = 0.2

# Canonical write target for homelab-engineer-proposed actions. The agent
# is repo-anchored to home-ops; every Class C+ change lands as a kubectl
# apply (Flux reconciliation downstream picks it up from git, but the
# direct-apply path is what errand-runner brokers). Mirrors
# smart_home_operator._HA_WRITE_TARGET.
_K8S_WRITE_TARGET = "kubectl-mcp.kubectl_apply"

# action_class values that imply a side effect requiring approval. Class A
# (read-only analysis) and B (vault-local commit) never need a user verdict;
# Class C (push/rollout) and D (apply directly) do.
_APPROVAL_REQUIRED_CLASSES: frozenset[ActionClass] = frozenset({"C", "D"})


class HomelabFinding(BaseModel):
    summary: str = Field(description="One-paragraph state-of-the-cluster for this request.")
    diagnosis: str = Field(description="Root-cause hypothesis if applicable; explicit blockers.")
    proposed_action: str = Field(description="What to do; tier and sequence if multi-step.")
    action_class: ActionClass = Field(
        description="A=read-only, B=local commit, C=push/rollout, D=apply directly."
    )
    handoff_target: Literal["user", "errand-runner", "coder"] = "user"
    target_repo: str = Field(default="home-ops")
    affected_resources: list[str] = Field(default_factory=list)
    references: list[str] = Field(
        default_factory=list,
        description="vault paths, repo paths + commit, kubectl outputs cited.",
    )
    # Optional verbatim rollback. Required in practice for Class C handoffs
    # to errand-runner (the broker escalates Class C without an undo path
    # to D); kept default-empty so existing analysis-only invocations
    # don't break. When present, the first line is folded into the
    # ApprovalRequest.undo_path the errand-runner sees.
    rollback: str = Field(
        default="",
        description=(
            "Mechanical rollback for Class C+ actions. Pre-change manifest "
            "/ kubectl-undo command verbatim — Rob must be able to paste it "
            "back without further help. Leave empty for analysis-only (A) "
            "findings."
        ),
    )


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(HomelabFinding)  # type: ignore[return-value]


def _render_markdown(finding: HomelabFinding, task_id: str) -> str:
    resources = "\n".join(f"- `{r}`" for r in finding.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in finding.references) or "_(none)_"
    rollback_section = (
        f"## Rollback (verbatim)\n\n```\n{finding.rollback}\n```\n\n"
        if finding.rollback.strip()
        else ""
    )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: homelab-finding\n"
        f"action_class: {finding.action_class}\n"
        f"handoff_target: {finding.handoff_target}\n"
        f"target_repo: {finding.target_repo}\n"
        "---\n\n"
        f"# Homelab finding — {finding.summary[:60]}\n\n"
        "## Summary\n\n"
        f"{finding.summary}\n\n"
        "## Diagnosis\n\n"
        f"{finding.diagnosis}\n\n"
        "## Proposed action\n\n"
        f"{finding.proposed_action}\n\n"
        f"{rollback_section}"
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


async def homelab_engineer_node(state: FleetState) -> dict[str, Any]:
    """Diagnose + propose for any home-ops cluster work."""
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
                "Produce a HomelabFinding. Stability bias: name SPOFs and "
                "blocking dependencies explicitly. Class C+ actions hand off "
                "to errand-runner with signed approval."
            )
        ),
    ]

    finding: HomelabFinding = model.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="homelab")

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
    # want this cluster change executed, please get the user's verdict."
    # Stability-bias is enforced by the persona — the LLM's job is to keep
    # Class C scoped (single resource bump, single label edit, single PVC).
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


def _compose_approval_request(finding: HomelabFinding) -> ApprovalRequest:
    """Translate a HomelabFinding into the ApprovalRequest errand-runner expects.

    Class C requires an undo path (errand-runner refuses Class C without one
    and escalates to D). For homelab changes we derive the undo by taking
    the finding's rollback text — verbatim — and prefixing it with the
    canonical write target. When the LLM omits `rollback` (default ""),
    undo_path stays None; errand-runner then escalates Class C → D at
    execute time, which is the safe behavior.

    payload_summary collapses the proposed action into the broker UI's
    single-line render. Truncated at 200 chars to keep Pushover / Zulip
    notification surfaces readable; the full draft lives in the vault and
    is linked from the broker message.
    """
    summary = finding.proposed_action.strip().splitlines()[0][:200]
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
