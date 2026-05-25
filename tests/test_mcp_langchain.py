"""Tests for the MCP → LangChain tool bridge.

Validates the boundary between `tools/mcp.py` (allowlist) and
`tools/mcp_langchain.py` (LangChain tool wrappers used by ReAct
agents). The MCP gateway itself is mocked — these tests are about
the wrapping logic, not real MCP traffic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.tools import BaseTool

from agents.tools.mcp import MCPCallError, MCPPermissionError, MCPResult
from agents.tools.mcp_langchain import build_mcp_tools_for_agent


def test_auditor_has_kubectl_tools() -> None:
    """auditor's allowlist contains kubectl-mcp read-only methods.
    The wrapper should produce one tool per capability."""
    tools = build_mcp_tools_for_agent("auditor")
    names = sorted(t.name for t in tools)
    # auditor's allowlist per tools/mcp.py is _READ_ONLY_KUBECTL.
    expected = {
        "kubectl_mcp__get",
        "kubectl_mcp__describe",
        "kubectl_mcp__logs",
        "kubectl_mcp__events",
        "kubectl_mcp__top",
    }
    assert expected.issubset(set(names))


def test_triager_has_no_tools() -> None:
    """triager has no MCP allowlist (vault-only by design)."""
    tools = build_mcp_tools_for_agent("triager")
    assert list(tools) == []


def test_tool_invocation_calls_mcp_gateway() -> None:
    """When the LLM calls the tool, it should go through MCPGatewayClient."""
    tools = build_mcp_tools_for_agent("auditor")
    kubectl_get = next(t for t in tools if t.name == "kubectl_mcp__get")

    fake_result = MCPResult(
        server="kubectl-mcp",
        method="get",
        status_code=200,
        data={"items": [{"name": "pod-a"}]},
    )

    with patch("agents.tools.mcp_langchain.MCPGatewayClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.call.return_value = fake_result
        MockClient.return_value = instance

        result = kubectl_get.invoke({"arguments": {"resource": "pods", "namespace": "ai"}})

    # Tool returns a stringified observation for the LLM.
    assert "pod-a" in result
    # MCPGatewayClient was constructed with the agent_id at module load
    # time + called with the (server, method, arguments) from the wrapper.
    instance.call.assert_called_once_with(
        "kubectl-mcp", "get",
        arguments={"resource": "pods", "namespace": "ai"},
    )


def test_tool_returns_permission_error_string_not_raise() -> None:
    """The ReAct loop is broken by Python exceptions. The wrapper must
    return a STRING the LLM can observe + reason about, not raise."""
    tools = build_mcp_tools_for_agent("auditor")
    kubectl_get = next(t for t in tools if t.name == "kubectl_mcp__get")

    with patch("agents.tools.mcp_langchain.MCPGatewayClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.call.side_effect = MCPPermissionError(
            "agent 'auditor' is not allowed to call kubectl-mcp.foo"
        )
        MockClient.return_value = instance

        result = kubectl_get.invoke({"arguments": {}})

    assert result.startswith("PERMISSION_DENIED")
    assert "not allowed" in result


def test_tool_returns_call_error_string() -> None:
    """5xx / transport errors return a structured string the LLM can act on."""
    tools = build_mcp_tools_for_agent("auditor")
    kubectl_get = next(t for t in tools if t.name == "kubectl_mcp__get")

    with patch("agents.tools.mcp_langchain.MCPGatewayClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.call.side_effect = MCPCallError("kubectl-mcp.get returned 503")
        MockClient.return_value = instance

        result = kubectl_get.invoke({"arguments": {}})

    assert result.startswith("MCP_CALL_ERROR")
    assert "503" in result


def test_long_response_truncated_for_context_budget() -> None:
    """Tool responses get truncated to fit the LLM's context budget."""
    tools = build_mcp_tools_for_agent("auditor")
    kubectl_get = next(t for t in tools if t.name == "kubectl_mcp__get")

    huge = {"data": "x" * 20000}
    fake_result = MCPResult(
        server="kubectl-mcp", method="get", status_code=200, data=huge,
    )

    with patch("agents.tools.mcp_langchain.MCPGatewayClient") as MockClient:
        instance = MagicMock()
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=None)
        instance.call.return_value = fake_result
        MockClient.return_value = instance

        result = kubectl_get.invoke({"arguments": {}})

    assert "[truncated;" in result
    # 8K cap + truncation note.
    assert len(result) < 9000


def test_each_tool_is_basetool_instance() -> None:
    """LangChain's create_react_agent expects BaseTool instances."""
    tools = build_mcp_tools_for_agent("homelab-engineer")
    assert tools
    for t in tools:
        assert isinstance(t, BaseTool)
        # Each must have a name + description (the LLM uses both).
        assert t.name
        assert t.description


@pytest.mark.parametrize(
    "agent_id,expected_min_count",
    [
        ("auditor", 5),         # kubectl read-only verbs
        ("homelab-engineer", 5),  # kubectl + prom + grafana + omada + netbox
        ("storage-operator", 3),  # kubectl + prom + grafana
        ("ml-operator", 3),
        ("network-operator", 3),
        ("observability-operator", 4),
        ("smart-home-operator", 5),
    ],
)
def test_agents_with_tools_get_tools(agent_id: str, expected_min_count: int) -> None:
    """Sanity check the wrap layer for every agent we expect to use it."""
    tools = build_mcp_tools_for_agent(agent_id)  # type: ignore[arg-type]
    assert len(tools) >= expected_min_count, (
        f"agent {agent_id} produced only {len(tools)} tools; "
        f"expected >= {expected_min_count}"
    )
