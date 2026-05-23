"""Agent node registry.

Single source of truth mapping each `AgentId` to its node-builder function.
Consumers should import `NODES` from here rather than re-importing each node
module by hand.

When adding a new specialist:

1. Add the agent_id to the `AgentId` Literal in ``agents.state``.
2. Create ``agents/nodes/<id>.py`` exporting ``<id>_node(state) -> dict``.
3. Add an ``"<id>": <id>_node`` entry below.

The registry test (``tests/test_node_registry.py``) asserts
``set(NODES) == set(get_args(AgentId))`` so any divergence between the
Literal and the dict fails CI loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .coder import coder_node
from .doc_writer import doc_writer_node
from .errand_runner import errand_runner_node
from .health_tracker import health_tracker_node
from .historian import historian_node
from .homelab_engineer import homelab_engineer_node
from .ml_operator import ml_operator_node
from .network_operator import network_operator_node
from .note_maker import note_maker_node
from .observability_operator import observability_operator_node
from .property_coordinator import property_coordinator_node
from .reporter import reporter_node
from .researcher import researcher_node
from .reviewer import reviewer_node
from .smart_home_operator import smart_home_operator_node
from .storage_operator import storage_operator_node
from .supervisor import supervisor_node
from .triager import triager_node

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from agents.state import AgentId, FleetState

    NodeBuilder = Callable[[FleetState], dict[str, Any]]


NODES: dict[AgentId, NodeBuilder] = {
    "triager": triager_node,
    "historian": historian_node,
    "note-maker": note_maker_node,
    "researcher": researcher_node,
    "coder": coder_node,
    "errand-runner": errand_runner_node,
    "supervisor": supervisor_node,
    "reviewer": reviewer_node,
    "homelab-engineer": homelab_engineer_node,
    "network-operator": network_operator_node,
    "storage-operator": storage_operator_node,
    "smart-home-operator": smart_home_operator_node,
    "ml-operator": ml_operator_node,
    "observability-operator": observability_operator_node,
    "health-tracker": health_tracker_node,
    "property-coordinator": property_coordinator_node,
    "doc-writer": doc_writer_node,
    "reporter": reporter_node,
}


__all__ = ["NODES"]
