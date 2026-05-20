"""researcher node — vault grep is real; LLM is mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agents.nodes.researcher import (
    ResearchFinding,
    SourceRef,
    _extract_search_terms,
    researcher_node,
)
from agents.state import FleetState


def _fake_finding() -> ResearchFinding:
    return ResearchFinding(
        summary="qwen2.5:14b is local-only, 9 GiB resident.",
        confidence="high",
        sources=[
            SourceRef(
                name="ollama model card",
                location="ollama.ai/library/qwen2.5",
                excerpt="14B param model, 9 GiB resident",
            ),
            SourceRef(
                name="home-ops memory",
                location="projects/home-ops/memory/project_p40_vram_budget.md",
                excerpt="P40 budget steady at 14.5 GiB across 6 consumers",
            ),
        ],
        caveats=["benchmarks not run on this hardware"],
        cross_references=["[[reference_gpu_resource_matrix]]"],
        open_follow_ups=["measure tokens/sec on P40 vs Spark"],
    )


def test_researcher_writes_finding_with_slug(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-200",
        source="zulip",
        content="how much VRAM does qwen2.5 14b need on the P40",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_finding()

    with patch("agents.nodes.researcher._build_llm", return_value=_FakeLLM()):
        result = researcher_node(state)

    found = list((temp_vault / "reports" / "research").glob("t-200-*.md"))
    assert len(found) == 1
    body = found[0].read_text(encoding="utf-8")
    assert "task_id: t-200" in body
    assert "confidence: high" in body
    assert "ollama model card" in body
    assert "reference_gpu_resource_matrix" in body
    assert "research complete" in result["output"]
    assert "sources=2" in result["output"]


def test_researcher_passes_vault_grep_context_to_llm(temp_vault: Path) -> None:
    """When vault content matches the question, the matching lines should be
    surfaced to the LLM in the prompt context."""
    # Plant a vault memory that matches the query
    proj = temp_vault / "projects" / "home-ops" / "memory"
    proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("P40 VRAM budget steady at 14.5 GiB across 6 consumers\n")

    state = FleetState(task_id="t-201", source="test", content="P40 VRAM budget")

    received_messages: list[object] = []

    class _SpyLLM:
        def invoke(self, messages):
            received_messages.extend(messages)
            return _fake_finding()

    with patch("agents.nodes.researcher._build_llm", return_value=_SpyLLM()):
        researcher_node(state)

    human_content = str(received_messages[1].content)  # type: ignore[attr-defined]
    assert "MEMORY.md:1" in human_content
    assert "P40 VRAM budget" in human_content


def test_extract_search_terms_drops_stopwords() -> None:
    terms = _extract_search_terms("what is the current state of frigate detection")
    assert "frigate" in [t.lower() for t in terms]
    assert "detection" in [t.lower() for t in terms]
    assert "what" not in [t.lower() for t in terms]
    assert "current" in [t.lower() for t in terms]


def test_extract_search_terms_caps_at_six() -> None:
    terms = _extract_search_terms("alpha beta gamma delta epsilon zeta eta theta iota kappa")
    assert len(terms) == 6


def test_extract_search_terms_skips_short_tokens() -> None:
    terms = _extract_search_terms("is a fly so we go big")
    # All tokens < 4 chars should be dropped
    assert not terms
