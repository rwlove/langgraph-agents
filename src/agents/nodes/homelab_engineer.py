"""homelab-engineer — Kubernetes / GitOps / homelab operations.

Repo-anchored to home-ops. Stability bias (per the persona). Uses kubectl-mcp
read-only for diagnostics + prometheus-mcp / grafana-mcp for cluster health.
Class C+ side effects route through errand-runner with a signed approval.
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

_AGENT_ID = "homelab-engineer"
_MODEL = "qwen2.5:14b"
_TEMPERATURE = 0.2


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


def _build_llm() -> BaseChatModel:
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(HomelabFinding)  # type: ignore[return-value]


def _render_markdown(finding: HomelabFinding, task_id: str) -> str:
    resources = "\n".join(f"- `{r}`" for r in finding.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in finding.references) or "_(none)_"
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
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


def homelab_engineer_node(state: FleetState) -> dict[str, Any]:
    """Diagnose + propose for any home-ops cluster work."""
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
                "Produce a HomelabFinding. Stability bias: name SPOFs and "
                "blocking dependencies explicitly. Class C+ actions hand off "
                "to errand-runner with signed approval."
            )
        ),
    ]

    finding: HomelabFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="homelab")

    return {
        "output": (
            f"homelab finding: {result.path} "
            f"(class={finding.action_class}, handoff={finding.handoff_target})"
        ),
    }
