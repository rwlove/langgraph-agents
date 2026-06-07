"""Eligibility map sanity — the categorical Claude hard-pin."""

from __future__ import annotations

from typing import get_args

from agents.state import AgentId
from evals.registry import CLAUDE_INELIGIBLE, is_claude_eligible


def test_health_tracker_is_ineligible() -> None:
    assert not is_claude_eligible("health-tracker")


def test_a_normal_agent_is_eligible() -> None:
    assert is_claude_eligible("network-operator")


def test_ineligible_set_contains_only_valid_agent_ids() -> None:
    valid = set(get_args(AgentId))
    assert CLAUDE_INELIGIBLE <= valid
