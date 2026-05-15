"""The fleet graph compiles and routes correctly to all 13 specialists.

Phase 1: triager's LLM is mocked (no ollama needed for the test). We exercise
the routing edge for every valid target_agent value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from agents.graphs.fleet import build_fleet_graph
from agents.state import ALL_AGENT_IDS, FleetState, TriageDecision


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


def test_graph_compiles(temp_vault: Path) -> None:
    graph = build_fleet_graph(checkpointer=None)
    assert graph is not None


def test_graph_routes_to_each_specialist(temp_vault: Path) -> None:
    """For each non-triager agent ID, the graph should reach the _pending stub."""
    for target in ALL_AGENT_IDS:
        if target == "triager":
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
            assert final.get("output", "").startswith("PHASE-1 STUB"), (
                f"specialist stub not reached for {target}"
            )
