"""Schema invariants for the eval harness."""

from __future__ import annotations

from evals.schema import DimensionScores, GoldenSet


def test_dimension_total_sums() -> None:
    s = DimensionScores(correctness=5, completeness=4, safety_gate=3, actionability=2)
    assert s.total == 14


def test_golden_set_validates_from_dict() -> None:
    gs = GoldenSet.model_validate(
        {
            "agent_id": "network-operator",
            "tasks": [{"task_id": "t1", "content": "do a thing"}],
        }
    )
    assert gs.agent_id == "network-operator"
    assert gs.tasks[0].data_tier == "internal"  # default
