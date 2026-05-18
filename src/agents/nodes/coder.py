"""coder — general code work that doesn't fit a domain specialist.

Drafts the change (diff or full file) and a hand-off envelope. Never executes;
Rob runs the change locally via Claude Code, or errand-runner applies it via
MCP when the change is config-only.

Phase 8: structured Patch output + draft markdown for inbox/drafts/. No real
execution path — that's Rob's job or errand-runner's depending on the change
class.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.state import AgentId, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "coder"
_TEMPERATURE = 0.2

PatchFormat = Literal["unified-diff", "full-file"]


class CodeDraft(BaseModel):
    title: str = Field(description="< 60 chars. What this change does.")
    rationale: str = Field(description="One paragraph; why this change.")
    target_repo: str = Field(description="Repo name or relative path under ~/workspace/.")
    target_files: list[str]
    patch_format: PatchFormat
    patch: str = Field(description="The actual patch (unified-diff) or full-file contents.")
    apply_command: str = Field(description="Shell command Rob runs to apply.")
    test_command: str = Field(description="How to verify the change works.")
    rollback: str = Field(description="How to undo.")
    handoff_target: Literal["user", "errand-runner"] = "user"


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(CodeDraft)  # type: ignore[return-value]


def _render_markdown(draft: CodeDraft, task_id: str) -> str:
    files_block = "\n".join(f"- `{f}`" for f in draft.target_files)
    fence = "diff" if draft.patch_format == "unified-diff" else ""
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: code-draft\n"
        f"target_repo: {draft.target_repo}\n"
        f"patch_format: {draft.patch_format}\n"
        f"handoff_target: {draft.handoff_target}\n"
        "---\n\n"
        f"# {draft.title}\n\n"
        "## Rationale\n\n"
        f"{draft.rationale}\n\n"
        "## Target files\n\n"
        f"{files_block}\n\n"
        "## Patch\n\n"
        f"```{fence}\n{draft.patch}\n```\n\n"
        "## Apply\n\n"
        f"```sh\n{draft.apply_command}\n```\n\n"
        "## Verify\n\n"
        f"```sh\n{draft.test_command}\n```\n\n"
        "## Rollback\n\n"
        f"```sh\n{draft.rollback}\n```\n"
    )


def coder_node(state: FleetState) -> dict[str, Any]:
    """Produce a code draft + hand-off envelope. Never executes."""
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
                "Produce a CodeDraft. Scope tight — refactor and bug-fix are "
                "separate. If the request crosses into infra (k8s/Flux), set "
                "rejected status; that's homelab-engineer's domain."
            )
        ),
    ]

    draft: CodeDraft = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(draft, state.task_id)
    result = write_draft(state.task_id, markdown, kind="code")

    return {
        "output": (
            f"code drafted: {result.path} "
            f"(target={draft.target_repo}, files={len(draft.target_files)}, "
            f"handoff={draft.handoff_target})"
        ),
    }
