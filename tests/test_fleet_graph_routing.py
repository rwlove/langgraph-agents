"""Fleet graph compiles and all 13 specialist targets are reachable.

Phase 8: every agent has a real node. Tests mock each node's LLM so the
graph executes without ollama.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from agents.graphs.fleet import build_fleet_graph
from agents.state import ALL_AGENT_IDS, FleetState, TriageDecision


def _fake_triager_returning(target: str):
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


def _fake_specialist_output(name: str):
    """Fake the *_node function for any specialist; bypasses LLM."""
    def _node(state: FleetState) -> dict[str, Any]:
        return {"output": f"{name}: fake output"}
    return _node


def test_graph_compiles(temp_vault: Path) -> None:
    graph = build_fleet_graph(checkpointer=None)
    assert graph is not None


def test_every_specialist_target_is_reachable(temp_vault: Path) -> None:
    """For each non-triager agent ID, mock its node + the triager to route there."""
    for target in ALL_AGENT_IDS:
        if target == "triager":
            continue

        node_function_name = f"{target.replace('-', '_')}_node"

        with (
            patch(
                "agents.graphs.fleet.triager_node",
                _fake_triager_returning(target),
            ),
            patch(
                f"agents.graphs.fleet.{node_function_name}",
                _fake_specialist_output(target),
            ),
        ):
            graph = build_fleet_graph(checkpointer=None)
            initial = FleetState(
                task_id=f"t-{target}",
                source="test",
                content="anything",
            )
            final = graph.invoke(initial)

            assert final["target_agent"] == target, f"routing failed for {target}"
            assert (
                f"{target}: fake output" in final.get("output", "")
            ), f"specialist node not reached for {target}; got {final.get('output')!r}"
