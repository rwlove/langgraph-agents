"""Golden sets + rubrics load and are well-formed."""

from __future__ import annotations

from typing import get_args

from agents.state import AgentId
from evals.loader import available_agents, load_golden_for, load_rubric

_VALID_IDS = set(get_args(AgentId))


def test_network_operator_golden_set_present() -> None:
    assert "network-operator" in available_agents()


def test_every_golden_set_is_well_formed() -> None:
    for agent_id in available_agents():
        gs = load_golden_for(agent_id)
        # Filename stem must match the declared agent_id, and be a real agent.
        assert gs.agent_id == agent_id
        assert gs.agent_id in _VALID_IDS
        assert gs.tasks, f"{agent_id} has no tasks"
        ids = [t.task_id for t in gs.tasks]
        assert len(ids) == len(set(ids)), f"{agent_id} has duplicate task_ids"
        for task in gs.tasks:
            assert task.content.strip(), f"{agent_id}/{task.task_id} empty content"
            # Restricted tasks never escalate at runtime — a Claude comparison
            # would be meaningless, so golden sets must not contain them.
            assert task.data_tier != "restricted", (
                f"{agent_id}/{task.task_id} is restricted-tier; "
                "restricted tasks can't be A/B'd against Claude"
            )


def test_rubric_specific_then_default() -> None:
    specific = load_rubric("network-operator")
    assert "network-operator" in specific
    # An agent with no override falls back to the default rubric.
    fallback = load_rubric("triager")
    assert "Default rubric" in fallback
