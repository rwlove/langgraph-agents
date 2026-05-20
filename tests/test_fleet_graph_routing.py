"""Fleet graph compiles and all 13 specialist targets are reachable.

Phase 8: every agent has a real node. Tests mock each node's LLM so the
graph executes without ollama.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver

from agents.graphs.fleet import build_fleet_graph
from agents.nodes import NODES
from agents.state import ALL_AGENT_IDS, ApprovalRequest, FleetState, TriageDecision


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
    """For each non-triager agent ID, mock its node + the triager to route there.

    Post node-registry refactor: graph builder iterates `NODES.items()` at
    build time, so patching `NODES[<id>]` swaps the function the builder
    sees. We patch the dict entries directly rather than re-imported
    symbols (which the registry no longer exposes from fleet.py).
    """
    for target in ALL_AGENT_IDS:
        if target == "triager":
            continue

        with (
            patch.dict(
                NODES,
                {
                    "triager": _fake_triager_returning(target),
                    target: _fake_specialist_output(target),
                },
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


# ---------------------------------------------------------------------------
# Specialist → errand-runner approval-compose routing.
#
# Validates the end-to-end PR #36 pattern this PR demonstrates:
#   triager → smart-home-operator (composes ApprovalRequest, sets
#     target_agent="errand-runner") → errand-runner pauses on interrupt().
#
# Uses an in-memory checkpointer; the interrupt surfaces as the
# `__interrupt__` key in the returned partial state (same shape exercised
# by tests/test_errand_runner.py::test_interrupt_pauses_when_approval_granted_unset).
# ---------------------------------------------------------------------------


def _fake_specialist_composing_approval(name: str):
    """Mimics a specialist that proposes a Class-C action — the canonical
    propose-then-execute shape. Identical to what smart-home-operator's
    `_compose_approval_request` builds for an HA write."""
    def _node(state: FleetState) -> dict[str, Any]:
        return {
            "output": f"{name}: composed approval_request",
            "approval_request": ApprovalRequest(
                action_class="C",
                target="ha-mcp.call_service",
                payload_summary="turn off the porch light",
                undo_path="ha-mcp.call_service: light.turn_on on light.porch",
                proposed_by="smart-home-operator",
            ),
            "target_agent": "errand-runner",
        }
    return _node


def test_specialist_with_approval_routes_to_errand_runner(temp_vault: Path) -> None:
    """When a specialist sets target_agent='errand-runner' + approval_request,
    `_route_after_specialist` routes to errand-runner (not END), and the
    errand-runner node pauses on interrupt() because approval_granted is
    unset. The pause surfaces as `__interrupt__` in the returned state."""
    with patch.dict(
        NODES,
        {
            "triager": _fake_triager_returning("smart-home-operator"),
            "smart-home-operator": _fake_specialist_composing_approval(
                "smart-home-operator"
            ),
        },
    ):
        graph = build_fleet_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "t-route-approval-1"}}
        initial = FleetState(
            task_id="t-route-approval-1",
            source="test",
            content="turn off the porch light",
        )
        result = graph.invoke(initial, config=config)

    # The interrupt() in errand-runner surfaces as __interrupt__ in the
    # returned partial state — proof the specialist → errand-runner route
    # fired AND the errand-runner ran to the interrupt point.
    assert "__interrupt__" in result, (
        f"errand-runner did not pause on interrupt; got keys: {list(result.keys())}"
    )
    interrupts = result["__interrupt__"]
    assert len(interrupts) == 1
    iv = interrupts[0].value
    assert iv["target"] == "ha-mcp.call_service"
    assert iv["action_class"] == "C"
    assert iv["proposed_by"] == "smart-home-operator"


def test_specialist_without_approval_still_ends(temp_vault: Path) -> None:
    """A specialist that doesn't populate approval_request still routes to
    END (no regression for the question/research path)."""
    with patch.dict(
        NODES,
        {
            "triager": _fake_triager_returning("smart-home-operator"),
            "smart-home-operator": _fake_specialist_output("smart-home-operator"),
        },
    ):
        graph = build_fleet_graph(checkpointer=None)
        initial = FleetState(
            task_id="t-route-noapproval-1",
            source="test",
            content="why didn't the porch light come on?",
        )
        final = graph.invoke(initial)

    # Should NOT pause; should NOT touch errand-runner; should END after
    # the specialist's output.
    assert "__interrupt__" not in final
    assert "smart-home-operator: fake output" in final.get("output", "")
    # No approval flow → target_agent stays at what triager set it to.
    assert final.get("approval_request") is None
