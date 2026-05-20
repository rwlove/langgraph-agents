"""researcher — gathers facts before action; produces a structured findings doc.

Vault-first search strategy per the persona: grep `projects/*/memory/` and
`user/memory/` for the topic. Phase 2: vault-only. Web search via searxng-mcp
lands in phase 4 once the MCP gateway client is wired.

Outputs a markdown findings file at `vault/reports/research/<task_id>-<slug>.md`
and a summary line in state.output for the orchestrator.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.state import AgentId, FleetState
from agents.tools.obsidian import VaultGrepHit, grep_vault_memory, write_finding

_AGENT_ID: AgentId = "researcher"
_TEMPERATURE = 0.2

Confidence = Literal["high", "medium", "low", "inconclusive"]


class SourceRef(BaseModel):
    name: str = Field(description="Short label, e.g. 'home-ops MEMORY.md' or vendor name.")
    location: str = Field(description="URL, vault path, or repo path + commit ref.")
    excerpt: str = Field(description="One-line summary of what this source says.")


class ResearchFinding(BaseModel):
    summary: str = Field(description="One paragraph; what's true, with confidence.")
    confidence: Confidence
    sources: list[SourceRef]
    caveats: list[str] = Field(
        default_factory=list,
        description="What's uncertain, conflicting, or unverified.",
    )
    cross_references: list[str] = Field(
        default_factory=list,
        description="Existing memory files or repo paths that relate.",
    )
    open_follow_ups: list[str] = Field(
        default_factory=list,
        description="Questions surfaced during research worth asking.",
    )


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(ResearchFinding)  # type: ignore[return-value]


def _extract_search_terms(content: str) -> list[str]:
    """Heuristic: pull out distinct noun-ish tokens of length >= 4. Vault search
    is small + local so over-broad queries cost little; we'd rather see one
    extra hit than miss the right memory."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{3,}\b", content):
        lower = tok.lower()
        if lower in seen or lower in _STOPWORDS:
            continue
        seen.add(lower)
        out.append(tok)
    return out[:6]  # cap; the LLM will see the matching lines anyway


_STOPWORDS: frozenset[str] = frozenset(
    {
        "what",
        "where",
        "when",
        "which",
        "have",
        "this",
        "that",
        "with",
        "from",
        "into",
        "should",
        "would",
        "could",
        "about",
        "there",
        "their",
        "they",
        "your",
        "yours",
        "mine",
        "ours",
        "rob",
        "user",
        "claude",
        "agent",
        "please",
        "thank",
        "thanks",
        "hello",
        "okay",
    }
)


def _format_vault_context(hits: list[VaultGrepHit]) -> str:
    if not hits:
        return "(no vault matches for derived search terms)"
    lines = [f"- {hit.path.name}:{hit.line_number} — {hit.excerpt()}" for hit in hits[:30]]
    return "\n".join(lines)


def _render_markdown(finding: ResearchFinding, task_id: str, question: str) -> str:
    sources_block = (
        "\n".join(
            f"{i}. **{s.name}** — `{s.location}`\n   {s.excerpt}"
            for i, s in enumerate(finding.sources, 1)
        )
        or "_(no sources surfaced)_"
    )

    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "_(none)_"

    return (
        "---\n"
        f"task_id: {task_id}\n"
        f"question: {question}\n"
        f"confidence: {finding.confidence}\n"
        "status: complete\n"
        "---\n\n"
        f"# Research: {question}\n\n"
        "## Findings\n\n"
        f"{finding.summary}\n\n"
        "## Sources\n\n"
        f"{sources_block}\n\n"
        "## Caveats\n\n"
        f"{_bullets(finding.caveats)}\n\n"
        "## Cross-references\n\n"
        f"{_bullets(finding.cross_references)}\n\n"
        "## Open follow-ups\n\n"
        f"{_bullets(finding.open_follow_ups)}\n"
    )


def researcher_node(state: FleetState) -> dict[str, Any]:
    """Gather facts, produce a findings file. Vault-first."""
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    terms = _extract_search_terms(state.content)
    vault_hits: list[VaultGrepHit] = []
    for term in terms:
        vault_hits.extend(grep_vault_memory(term, max_hits=10))
    vault_context = _format_vault_context(vault_hits)

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"RESEARCH QUESTION:\n\n{state.content}\n\n"
                f"VAULT CONTEXT (grep over projects/*/memory + user/memory):\n"
                f"{vault_context}\n\n"
                "Produce a structured ResearchFinding. Cite every claim. If "
                "the vault context already answers it, prefer those sources "
                "over speculation."
            )
        ),
    ]

    finding: ResearchFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, task_id=state.task_id, question=state.content)
    result = write_finding(state.task_id, topic=state.content, body=markdown)

    return {
        "output": (
            f"research complete: {result.path} "
            f"(confidence={finding.confidence}, sources={len(finding.sources)})"
        ),
    }
