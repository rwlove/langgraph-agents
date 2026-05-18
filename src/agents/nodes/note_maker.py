"""note-maker — drafts a structured note from raw inbox content.

Takes free-form text (often a voice transcript), produces a markdown draft
with frontmatter, writes it to `vault/inbox/drafts/note-<task_id>.md`. Never
writes to the personal vault directly — that's a user-only action.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.state import AgentId, Domain, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "note-maker"
_TEMPERATURE = 0.3


class NoteDraft(BaseModel):
    """Structured note output. Rendered to markdown before write."""

    title: str = Field(description="Concise, < 60 chars. No PHI in title.")
    domain: Domain
    body: str = Field(description="Domain-appropriate voice, structured prose.")
    tags: list[str] = Field(default_factory=list)
    proposed_location: str = Field(
        description="Suggested path under ~/vaults/personal/. Suggestion only."
    )
    related: list[str] = Field(
        default_factory=list,
        description="Optional cross-references: [[note-name]] or path/to/file",
    )
    new_vs_append: Literal["new", "append", "ambiguous"] = "new"


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(NoteDraft)  # type: ignore[return-value]


def _render_markdown(draft: NoteDraft, task_id: str, source_hint: str) -> str:
    tags_line = " ".join(f"#{t.lstrip('#')}" for t in draft.tags) if draft.tags else ""
    related_block = ""
    if draft.related:
        related_block = "\n\n## Related\n" + "\n".join(f"- {r}" for r in draft.related)

    return (
        "---\n"
        f"task_id: {task_id}\n"
        f"source: {source_hint}\n"
        f"domain: {draft.domain}\n"
        "intent: note\n"
        f"proposed_location: {draft.proposed_location}\n"
        f"new_vs_append: {draft.new_vs_append}\n"
        "status: drafted\n"
        "---\n\n"
        f"# {draft.title}\n\n"
        f"{draft.body}\n"
        + (f"\n## Tags\n{tags_line}\n" if tags_line else "")
        + related_block
        + "\n"
    )


def note_maker_node(state: FleetState) -> dict[str, Any]:
    """Draft a note from inbox content. Writes to vault/inbox/drafts/."""
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    triage_hint = ""
    if state.triage:
        triage_hint = (
            f"\n\nTRIAGE CONTEXT:\n"
            f"- domain: {state.triage.domain}\n"
            f"- intent: {state.triage.intent}\n"
            f"- summary: {state.triage.summary}\n"
        )

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                "INBOX ENTRY (clean up, organize, draft a note):\n\n"
                f"{state.content}{triage_hint}\n\n"
                "Produce the structured NoteDraft. The body should be polished "
                "but preserve Rob's actual phrasing where it matters. If voice "
                "transcript fillers are present, strip them."
            )
        ),
    ]

    draft: NoteDraft = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(draft, task_id=state.task_id, source_hint=state.source)
    result = write_draft(state.task_id, markdown, kind="note")

    return {
        "output": (
            f"note drafted: {result.path} "
            f"({result.bytes_written} bytes, domain={draft.domain})"
        ),
    }
