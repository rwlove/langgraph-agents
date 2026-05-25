"""Tests for the MCP → LangChain tool bridge.

The previous version of this module wrapped a custom MCPGatewayClient.
PR-T rewrote it to use `langchain-mcp-adapters` against the live
Kuadrant MCP gateway (Streamable HTTP transport). These tests
mock the upstream discovery so we can assert filtering logic
without hitting the network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.tools import mcp_langchain


@pytest.fixture(autouse=True)
def reset_tool_cache():
    """Each test gets a clean module-level cache."""
    mcp_langchain._TOOL_CACHE = None
    yield
    mcp_langchain._TOOL_CACHE = None


def _fake_tool(name: str) -> MagicMock:
    """Minimal stand-in for a `langchain_core.tools.BaseTool`."""
    t = MagicMock()
    t.name = name
    return t


@pytest.mark.asyncio
async def test_auditor_gets_curated_subset() -> None:
    """auditor's curated set in `_AGENT_TOOL_NAMES` filters the gateway catalog."""
    fake_catalog = [
        _fake_tool("kubectl_get_pods"),
        _fake_tool("kubectl_get_deployments"),
        _fake_tool("kubectl_describe"),
        _fake_tool("kubectl_get_events"),
        _fake_tool("kubectl_get_logs"),
        _fake_tool("kubectl_get_namespaces"),
        _fake_tool("kubectl_get_things_we_dont_want"),  # NOT in auditor's set
        _fake_tool("ha_ha_call_service"),  # write tool; NOT in auditor's set
    ]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("auditor")
    names = sorted(t.name for t in tools)
    assert names == [
        "kubectl_describe",
        "kubectl_get_deployments",
        "kubectl_get_events",
        "kubectl_get_logs",
        "kubectl_get_namespaces",
        "kubectl_get_pods",
    ]


@pytest.mark.asyncio
async def test_network_operator_gets_curated_subset() -> None:
    """network-operator gets Omada + Netbox read-only tools; no write tools."""
    fake_catalog = [
        _fake_tool("omada_listSites"),
        _fake_tool("omada_listDevices"),
        _fake_tool("omada_getSsidList"),
        _fake_tool("omada_getLanNetworkList"),
        _fake_tool("omada_getGatewayWanStatus"),
        _fake_tool("omada_getFirewallSetting"),
        _fake_tool("omada_listSiteAlerts"),
        _fake_tool("omada_listSiteEvents"),
        _fake_tool("netbox_netbox_get_objects"),
        _fake_tool("netbox_netbox_search_objects"),
        _fake_tool("netbox_netbox_get_object_by_id"),
        _fake_tool("omada_setFirewallRule"),   # write — NOT in set
        _fake_tool("kubectl_get_pods"),        # kubectl — NOT in network-operator set
    ]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("network-operator")
    names = sorted(t.name for t in tools)
    assert names == [
        "netbox_netbox_get_object_by_id",
        "netbox_netbox_get_objects",
        "netbox_netbox_search_objects",
        "omada_getFirewallSetting",
        "omada_getGatewayWanStatus",
        "omada_getLanNetworkList",
        "omada_getSsidList",
        "omada_listDevices",
        "omada_listSiteAlerts",
        "omada_listSiteEvents",
        "omada_listSites",
    ]


@pytest.mark.asyncio
async def test_storage_operator_gets_curated_subset() -> None:
    """storage-operator gets kubectl PVC/PV/StorageClass + supporting tools."""
    fake_catalog = [
        _fake_tool("kubectl_get_pvcs"),
        _fake_tool("kubectl_get_persistent_volumes"),
        _fake_tool("kubectl_get_storage_classes"),
        _fake_tool("kubectl_get_pods"),
        _fake_tool("kubectl_get_deployments"),
        _fake_tool("kubectl_get_namespaces"),
        _fake_tool("kubectl_describe"),
        _fake_tool("kubectl_get_events"),
        _fake_tool("kubectl_delete_pvc"),     # write — NOT in set
        _fake_tool("grafana_query_prometheus"),  # observability — NOT in set
    ]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("storage-operator")
    names = sorted(t.name for t in tools)
    assert names == [
        "kubectl_describe",
        "kubectl_get_deployments",
        "kubectl_get_events",
        "kubectl_get_namespaces",
        "kubectl_get_persistent_volumes",
        "kubectl_get_pods",
        "kubectl_get_pvcs",
        "kubectl_get_storage_classes",
    ]


@pytest.mark.asyncio
async def test_observability_operator_gets_curated_subset() -> None:
    """observability-operator gets Grafana/Prometheus/Loki + kubectl supplement."""
    fake_catalog = [
        _fake_tool("grafana_query_prometheus"),
        _fake_tool("grafana_list_prometheus_metric_names"),
        _fake_tool("grafana_query_loki_logs"),
        _fake_tool("grafana_list_loki_label_names"),
        _fake_tool("grafana_get_alert_group"),
        _fake_tool("grafana_list_alert_groups"),
        _fake_tool("grafana_search_dashboards"),
        _fake_tool("grafana_get_dashboard_summary"),
        _fake_tool("kubectl_get_pods"),
        _fake_tool("kubectl_get_events"),
        _fake_tool("grafana_create_dashboard"),   # write — NOT in set
        _fake_tool("ha_ha_get_state"),            # HA — NOT in set
    ]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("observability-operator")
    names = sorted(t.name for t in tools)
    assert names == [
        "grafana_get_alert_group",
        "grafana_get_dashboard_summary",
        "grafana_list_alert_groups",
        "grafana_list_loki_label_names",
        "grafana_list_prometheus_metric_names",
        "grafana_query_loki_logs",
        "grafana_query_prometheus",
        "grafana_search_dashboards",
        "kubectl_get_events",
        "kubectl_get_pods",
    ]


@pytest.mark.asyncio
async def test_homelab_engineer_gets_curated_subset() -> None:
    """homelab-engineer gets the broadest kubectl surface of all operators."""
    fake_catalog = [
        _fake_tool("kubectl_get_pods"),
        _fake_tool("kubectl_get_deployments"),
        _fake_tool("kubectl_get_namespaces"),
        _fake_tool("kubectl_get_nodes"),
        _fake_tool("kubectl_describe"),
        _fake_tool("kubectl_get_events"),
        _fake_tool("kubectl_get_logs"),
        _fake_tool("kubectl_health_check"),
        _fake_tool("kubectl_get_statefulsets"),
        _fake_tool("kubectl_get_daemonsets"),
        _fake_tool("kubectl_get_configmaps"),
        _fake_tool("kubectl_get_services"),
        _fake_tool("kubectl_delete_pod"),     # write — NOT in set
        _fake_tool("ha_ha_call_service"),     # HA — NOT in set
    ]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("homelab-engineer")
    names = sorted(t.name for t in tools)
    assert names == [
        "kubectl_describe",
        "kubectl_get_configmaps",
        "kubectl_get_daemonsets",
        "kubectl_get_deployments",
        "kubectl_get_events",
        "kubectl_get_logs",
        "kubectl_get_namespaces",
        "kubectl_get_nodes",
        "kubectl_get_pods",
        "kubectl_get_services",
        "kubectl_get_statefulsets",
        "kubectl_health_check",
    ]


@pytest.mark.asyncio
async def test_smart_home_operator_gets_curated_subset() -> None:
    """smart-home-operator gets read-only HA tools; no call_service write tool."""
    fake_catalog = [
        _fake_tool("ha_ha_get_state"),
        _fake_tool("ha_ha_get_entity"),
        _fake_tool("ha_ha_search_entities"),
        _fake_tool("ha_ha_get_history"),
        _fake_tool("ha_ha_get_logs"),
        _fake_tool("ha_ha_get_overview"),
        _fake_tool("ha_ha_get_system_health"),
        _fake_tool("ha_ha_list_services"),
        _fake_tool("ha_ha_get_device"),
        _fake_tool("ha_ha_call_service"),   # write — NOT in set
        _fake_tool("kubectl_get_pods"),     # kubectl — NOT in smart-home set
    ]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("smart-home-operator")
    names = sorted(t.name for t in tools)
    assert names == [
        "ha_ha_get_device",
        "ha_ha_get_entity",
        "ha_ha_get_history",
        "ha_ha_get_logs",
        "ha_ha_get_overview",
        "ha_ha_get_state",
        "ha_ha_get_system_health",
        "ha_ha_list_services",
        "ha_ha_search_entities",
    ]


@pytest.mark.asyncio
async def test_agent_without_curated_set_gets_empty_list() -> None:
    """Agents not in `_AGENT_TOOL_NAMES` get no tools (intentional v1 scope)."""
    # No catalog discovery should happen — early return.
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(),
    ) as m_discover:
        tools = await mcp_langchain.build_mcp_tools_for_agent("triager")
    assert list(tools) == []
    m_discover.assert_not_called()


@pytest.mark.asyncio
async def test_missing_tools_in_catalog_silently_skipped() -> None:
    """If a curated name isn't in the gateway catalog (e.g. MCP server offline),
    we skip it rather than crash. Agent operates with what's available."""
    # Only one of auditor's wanted tools is in the fake catalog.
    fake_catalog = [_fake_tool("kubectl_get_pods")]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("auditor")
    assert [t.name for t in tools] == ["kubectl_get_pods"]


@pytest.mark.asyncio
async def test_discovery_failure_returns_empty_not_raise() -> None:
    """Gateway unreachable → empty tool list, NOT exception. The agent's
    ReAct loop should still construct cleanly; it just has nothing to call."""
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(side_effect=RuntimeError("gateway timed out")),
    ):
        tools = await mcp_langchain.build_mcp_tools_for_agent("auditor")
    assert list(tools) == []
    # And the failure-poisoned cache: subsequent calls don't retry.
    # (Retry semantics intentional: discovery failure usually means
    # something's wrong cluster-wide; spamming retries from every agent
    # invocation makes it worse.)
    assert mcp_langchain._TOOL_CACHE == []


@pytest.mark.asyncio
async def test_cache_hydrated_once() -> None:
    """Repeated calls hit the cache, not the discovery path."""
    fake_catalog = [_fake_tool("kubectl_get_pods")]
    with patch(
        "agents.tools.mcp_langchain._discover_tools_async",
        new=AsyncMock(return_value=fake_catalog),
    ) as m_discover:
        await mcp_langchain.build_mcp_tools_for_agent("auditor")
        await mcp_langchain.build_mcp_tools_for_agent("auditor")
        await mcp_langchain.build_mcp_tools_for_agent("auditor")
    m_discover.assert_called_once()


def test_gateway_url_construction() -> None:
    """The lib needs `/mcp` appended to the base gateway URL."""
    url = mcp_langchain._build_gateway_url()
    assert url.endswith("/mcp")
    assert "://" in url  # actually a URL, not a path fragment
