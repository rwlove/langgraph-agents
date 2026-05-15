"""health-tracker — sensitive medical content. Local-only model. NEVER Claude.

This module deliberately does NOT import anthropic. The test in
tests/test_health_tracker_isolation.py asserts this invariant statically.

Drafts medical notes from voice transcripts / inbox content. Every output is
Class C (approval-gated). Writes drafts to vault/inbox/drafts/medical-<id>.md
which is Linux-only (the claude vault doesn't sync to Android). Rob publishes
manually to ~/vaults/personal/medical/.

Never logs medical content in the activity log — metadata only.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID = "health-tracker"
_MODEL = "qwen2.5:14b"
_TEMPERATURE = 0.2

NoteKind = Literal["visit", "metric", "rx", "symptom", "general"]


class MedicalDraft(BaseModel):
    title: str = Field(description="Concise; no PHI in title (no provider names, no diagnoses).")
    kind: NoteKind
    body: str = Field(description="Structured per kind. Preserve Rob's phrasing.")
    tags: list[str] = Field(
        default_factory=list,
        description="Domain tags only — no provider names, no specific medications by brand.",
    )
    proposed_publish_path: str = Field(
        description="Suggested path under ~/vaults/personal/medical/. Rob publishes manually."
    )


def _build_llm() -> BaseChatModel:
    """Local-only model. This function deliberately uses ChatOllama and only
    ChatOllama — no other provider is reachable from this module."""
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(MedicalDraft)  # type: ignore[return-value]


def _render_markdown(draft: MedicalDraft, task_id: str) -> str:
    tags = " ".join(f"#{t.lstrip('#')}" for t in draft.tags) if draft.tags else ""
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "domain: medical\n"
        "visibility: user-only\n"
        f"kind: {draft.kind}\n"
        f"proposed_publish_path: {draft.proposed_publish_path}\n"
        "status: drafted-for-review\n"
        "---\n\n"
        f"# {draft.title}\n\n"
        f"{draft.body}\n"
        + (f"\n## Tags\n{tags}\n" if tags else "")
    )


def health_tracker_node(state: FleetState) -> dict[str, Any]:
    """Draft a medical note. Local-only model enforced architecturally."""
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    # NB: We deliberately do NOT include state.triage.summary or .reasoning in
    # the prompt context — the triager's structured output may leak content
    # into metadata fields. Health-tracker reads `state.content` only.
    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"INBOX (medical content):\n\n{state.content}\n\n"
                "Produce a MedicalDraft. No PHI in title. Tags are domain-"
                "level only — no provider names, no brand-name medications. "
                "Preserve Rob's actual phrasing for facts; clean up filler."
            )
        ),
    ]

    draft: MedicalDraft = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(draft, state.task_id)
    result = write_draft(state.task_id, markdown, kind="medical")

    # METADATA ONLY in the return — never the draft content.
    return {
        "output": f"medical drafted: {result.path} (kind={draft.kind})",
    }
