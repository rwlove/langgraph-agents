"""doc-writer — drafts README / docs / ADR updates.

Triggered explicitly or by a sweep that finds a repo with N commits since
its last doc-touched commit. Output is a unified-diff draft + apply command
written to vault inbox/drafts/. Never executes the diff.
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

_AGENT_ID: AgentId = "doc-writer"
_TEMPERATURE = 0.2

DocKind = Literal["readme", "docs", "adr", "changelog", "release-notes", "inline-comment"]


class DocsDraft(BaseModel):
    title: str = Field(description="< 60 chars. What the update is.")
    why: str = Field(description="One paragraph; the gap this fills.")
    target_repo: str = Field(description="Repo name or path under ~/workspace/.")
    target_file: str = Field(description="Path inside the repo (e.g. README.md).")
    kind: DocKind
    patch: str = Field(description="Unified diff. Surgical edits preferred over rewrites.")
    apply_command: str = Field(description="Shell command Rob runs to apply.")
    rollback: str = Field(description="How to undo the doc change.")
    action_class: ActionClass = "B"  # draft only by default
    handoff_target: Literal["user", "errand-runner", "coder"] = "user"


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(DocsDraft)  # type: ignore[return-value]


def _render_markdown(draft: DocsDraft, task_id: str) -> str:
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: docs-draft\n"
        f"doc_kind: {draft.kind}\n"
        f"target_repo: {draft.target_repo}\n"
        f"target_file: {draft.target_file}\n"
        f"handoff_target: {draft.handoff_target}\n"
        "---\n\n"
        f"# Doc update: {draft.title}\n\n"
        "## Why\n\n"
        f"{draft.why}\n\n"
        "## Diff\n\n"
        f"```diff\n{draft.patch}\n```\n\n"
        "## How to apply\n\n"
        f"```sh\n{draft.apply_command}\n```\n\n"
        "## Rollback\n\n"
        f"```sh\n{draft.rollback}\n```\n"
    )


def doc_writer_node(state: FleetState) -> dict[str, Any]:
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
                "Produce a DocsDraft. Surgical patches preferred — match the "
                "target file's existing structure. No aspirational claims; "
                "document what's actually there. If a fact is unclear from "
                "the code, say so in the doc rather than guessing."
            )
        ),
    ]

    draft: DocsDraft = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(draft, state.task_id)
    result = write_draft(state.task_id, markdown, kind="docs")

    return {
        "output": (
            f"docs drafted: {result.path} "
            f"(target={draft.target_repo}/{draft.target_file}, kind={draft.kind})"
        ),
    }
