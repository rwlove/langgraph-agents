"""NODES dict consistency with the AgentId Literal.

If you add an entry to ``AgentId`` but forget to register a node function in
``agents/nodes/__init__.py`` (or vice versa), this test fails loudly. Without
it, divergence is silent — fleet graph misses an agent, /v1/models drops one,
etc.
"""

from __future__ import annotations

from typing import get_args

from agents.nodes import NODES
from agents.state import AgentId


def test_nodes_dict_matches_agent_id_literal() -> None:
    literal_ids = set(get_args(AgentId))
    dict_ids = set(NODES.keys())

    missing_from_dict = literal_ids - dict_ids
    missing_from_literal = dict_ids - literal_ids

    assert not missing_from_dict, (
        f"AgentId Literal has entries not in NODES dict: {sorted(missing_from_dict)}. "
        "Either add a node function + register it in agents/nodes/__init__.py, "
        "or remove the agent from the AgentId Literal."
    )
    assert not missing_from_literal, (
        f"NODES dict has entries not in AgentId Literal: {sorted(missing_from_literal)}. "
        "Add the agent_id to the AgentId Literal in agents/state.py."
    )


def test_nodes_dict_values_are_callable() -> None:
    for agent_id, fn in NODES.items():
        assert callable(fn), f"NODES[{agent_id!r}] is not callable: {fn!r}"
