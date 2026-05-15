"""Static assertions on the MCP gateway allowlists.

These are load-bearing per the security review: the "only errand-runner has
MCP write capability" rule is enforced here in code, not in cluster config.
"""

from __future__ import annotations

import pytest

from agents.state import ALL_AGENT_IDS
from agents.tools.mcp import (
    ALLOWLISTS,
    MCPGatewayClient,
    MCPPermissionError,
    agents_with_write_capability,
    is_allowed,
)


def test_every_agent_has_an_allowlist_entry() -> None:
    """Make sure we don't accidentally leave an agent without a (possibly
    empty) entry — that would default to fully-denied, which is correct
    *behavior* but hides intent."""
    for agent_id in ALL_AGENT_IDS:
        assert agent_id in ALLOWLISTS, f"missing allowlist entry for {agent_id}"


def test_only_errand_runner_has_write_capability() -> None:
    """The agent fleet's load-bearing security invariant."""
    writers = agents_with_write_capability()
    assert writers == {"errand-runner"}, (
        f"expected only errand-runner to have write capability; got {writers}"
    )


def test_health_tracker_has_only_local_capabilities() -> None:
    """Health-tracker must NEVER reach external services. Only paperless-mcp
    is allowed (read of medical-tagged docs). No searxng, no immich, no web."""
    caps = ALLOWLISTS["health-tracker"]
    servers = {c.server for c in caps}
    assert servers in ({"paperless-mcp"}, set()), (
        f"health-tracker has unexpected MCP servers: {servers}"
    )
    assert not any(c.write for c in caps), "health-tracker must be read-only"


def test_is_allowed_rejects_out_of_scope() -> None:
    assert is_allowed("homelab-engineer", "kubectl-mcp", "get")
    assert not is_allowed("homelab-engineer", "ha-mcp", "call_service")
    assert not is_allowed("note-maker", "kubectl-mcp", "get")


def test_client_refuses_out_of_scope_before_http() -> None:
    """The client must raise MCPPermissionError before any HTTP call."""
    with MCPGatewayClient("note-maker") as client:
        with pytest.raises(MCPPermissionError):
            client.call("ha-mcp", "call_service", arguments={})


def test_triager_reporter_reviewer_have_no_mcp() -> None:
    """These agents are vault/log-only by design."""
    for agent_id in ("triager", "reporter", "reviewer"):
        assert ALLOWLISTS[agent_id] == frozenset(), (
            f"{agent_id} should have no MCP capabilities"
        )
