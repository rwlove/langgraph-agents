"""The fleet graph compiles and routes correctly to all 13 specialists.

Phase 2: note-maker and researcher have real nodes; everything else routes
to the `_pending` stub. The triager is mocked in every case so the test
doesn't depend on ollama being available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from agents.graphs.fleet import build_fleet_graph
from agents.nodes.note_maker import NoteDraft
from agents.nodes.researcher import ResearchFinding, SourceRef
from agents.state import ALL_AGENT_IDS, FleetState, TriageDecision

# Real-node target IDs whose LLMs need to be mocked, not just the triager.
_REAL_NODE_TARGETS = {"note-maker", "researcher"}


def _fake_triager_returning(target: str):
    """Build a fake triager_node that returns a fixed routing decision."""

    def _node(state: FleetState) -> dict[str, Any]:
        decision = TriageDecision(
            summary="fake",
            domain="homelab",
            intent="question",
            target_agent=target,  # type: ignore[arg-type]
            confidence=0.95,
            reasoning="fake",
        )
        return {"triage": decision, "target_agent": target}

    return _node


class _FakeNoteMakerLLM:
    def invoke(self, _messages: object) -> NoteDraft:
        return NoteDraft(
            title="t",
            domain="homelab",
            body="b",
            proposed_location="~/x.md",
        )


class _FakeResearcherLLM:
    def invoke(self, _messages: object) -> ResearchFinding:
        return ResearchFinding(
            summary="s",
            confidence="medium",
            sources=[SourceRef(name="n", location="l", excerpt="e")],
        )


def test_graph_compiles(temp_vault: Path) -> None:
    graph = build_fleet_graph(checkpointer=None)
    assert graph is not None


def test_stubbed_specialists_route_to_pending(temp_vault: Path) -> None:
    """For each still-stubbed agent ID, the graph should reach the _pending stub."""
    for target in ALL_AGENT_IDS:
        if target == "triager" or target in _REAL_NODE_TARGETS:
            continue

        with patch("agents.graphs.fleet.triager_node", _fake_triager_returning(target)):
            graph = build_fleet_graph(checkpointer=None)
            initial = FleetState(
                task_id=f"t-{target}",
                source="test",
                content="anything",
            )
            final = graph.invoke(initial)

            assert final["target_agent"] == target, f"routing failed for {target}"
            output = final.get("output", "") or ""
            assert output.startswith("STUB:"), (
                f"specialist stub not reached for {target}; got: {output!r}"
            )


def test_note_maker_target_reaches_real_node(temp_vault: Path) -> None:
    with (
        patch("agents.graphs.fleet.triager_node", _fake_triager_returning("note-maker")),
        patch("agents.nodes.note_maker._build_llm", return_value=_FakeNoteMakerLLM()),
    ):
        graph = build_fleet_graph(checkpointer=None)
        final = graph.invoke(
            FleetState(task_id="t-nm", source="test", content="draft me a note"),
        )

    assert final["target_agent"] == "note-maker"
    output = final.get("output", "")
    assert output.startswith("note drafted")
    assert (temp_vault / "inbox" / "drafts" / "note-t-nm.md").exists()


def test_researcher_target_reaches_real_node(temp_vault: Path) -> None:
    with (
        patch("agents.graphs.fleet.triager_node", _fake_triager_returning("researcher")),
        patch("agents.nodes.researcher._build_llm", return_value=_FakeResearcherLLM()),
    ):
        graph = build_fleet_graph(checkpointer=None)
        final = graph.invoke(
            FleetState(task_id="t-r", source="test", content="research VRAM"),
        )

    assert final["target_agent"] == "researcher"
    output = final.get("output", "")
    assert output.startswith("research complete")
    matches = list((temp_vault / "reports" / "research").glob("t-r-*.md"))
    assert len(matches) == 1
