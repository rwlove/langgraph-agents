"""LangChain-tool view of the MCP gateway, via `langchain-mcp-adapters`.

## What this replaces

The previous version of this module wrapped `MCPGatewayClient.call()`
in custom `StructuredTool` objects. That client used a stale REST URL
pattern (`POST /servers/<server>/tools/<method>`) which the current
Kuadrant MCP gateway doesn't speak — every tool call returned
`400 invalid mcp request` against the live gateway. See memory
`[[reference_mcp_gateway_client_broken_2026_05_25]]`.

The current gateway speaks **MCP Streamable HTTP transport** at
`/mcp` (JSON-RPC + Mcp-Session-Id header + SSE responses). Rather
than implement that protocol by hand, we use the official
`langchain-mcp-adapters` library's `MultiServerMCPClient`, which
already handles session lifecycle + JSON-RPC framing + SSE parsing.

## Async / sync bridge

The lib is async-native: `await client.get_tools()` is the only way
to discover tools. We bridge to the existing sync `auditor_node` via
`asyncio.run()` at module load time — discovery happens once per
process, results are cached. Tool invocation inside the ReAct loop
also goes through the lib's async path; each LangChain tool's
sync `invoke()` runs `asyncio.run()` under the hood (the lib
provides both interfaces).

## Per-agent tool selection

The legacy `ALLOWLISTS` in `mcp.py` was structured around a
hypothetical MCP server model that doesn't match what the actual
gateway exposes. The live gateway has ~997 tools across families
(kubectl_*, prom_*, grafana_*, ha_ha_*, omada_*, netbox_*, etc.).
Dropping all 997 on an agent's context budget would blow it out
immediately.

For v1, each agent gets a **hand-curated subset** of the catalog
matching its job. The maps live below as `_AGENT_TOOL_NAMES`.
Adding tools to an agent is a single-source-of-truth code change
here; the ALLOWLISTS in `mcp.py` stays as the boundary doc for
errand-runner (which still uses the imperative API).

## Scope for PR-T

Wire auditor first; other agents land in follow-up PRs as their
tool sets get curated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from agents.settings import get_settings
from agents.state import AgentId

logger = logging.getLogger(__name__)

# Async lock prevents concurrent discovery races when multiple tasks
# start simultaneously before the cache is warm.
_TOOL_CACHE_LOCK = asyncio.Lock()


# Per-agent curated tool subsets. The names match what the live gateway
# returns from `MultiServerMCPClient.get_tools()`. Verified 2026-05-25
# against the cluster's mcp-gateway-istio service.
#
# Auditor gets read-only kubectl + targeted prometheus. Adding tools is
# a 1-line edit + redeploy. Removing one is a 1-line edit (no behavioral
# coupling to anything else).
_AGENT_TOOL_NAMES: dict[AgentId, frozenset[str]] = {
    "auditor": frozenset({
        # Image enumeration is the auditor's bread-and-butter v1 use case.
        # `kubectl_get_pods` + `kubectl_get_deployments` cover most surface;
        # logs + events let it look at recent context if a finding warrants it.
        "kubectl_get_pods",
        "kubectl_get_deployments",
        "kubectl_get_namespaces",
        "kubectl_describe",
        "kubectl_get_events",
        "kubectl_get_logs",
    }),
    # Other agents land in follow-up PRs as their toolsets get curated.
}


# Module-level tool cache. Populated on first call to
# `build_mcp_tools_for_agent`; reused for the process lifetime.
_TOOL_CACHE: list[BaseTool] | None = None


def _build_gateway_url() -> str:
    """Construct the Streamable HTTP MCP endpoint URL for the cluster's gateway.

    `settings.mcp_gateway_url` is the base URL the legacy
    `MCPGatewayClient` used (e.g. `http://mcp-gateway-istio...:8080`).
    The Kuadrant gateway exposes the MCP protocol at `/mcp` on that
    same host; we append the path here so the rest of the codebase
    doesn't need to know about it.
    """
    base = get_settings().mcp_gateway_url.rstrip("/")
    return f"{base}/mcp"


async def _discover_tools_async() -> list[BaseTool]:
    """Call the gateway's MCP `tools/list` once and return the full catalog."""
    client = MultiServerMCPClient({
        "gateway": {
            "url": _build_gateway_url(),
            "transport": "streamable_http",
        },
    })
    return await client.get_tools()


async def _ensure_tools_loaded_async() -> list[BaseTool]:
    """Hydrate the module-level tool cache on first call (async).

    Protected by an asyncio.Lock so concurrent callers don't race.
    If discovery raises (gateway unreachable, no MCP servers registered,
    etc.), logs and returns an empty list — the ReAct loop constructs
    cleanly with no tools rather than crashing.
    """
    global _TOOL_CACHE  # noqa: PLW0603 — intentional module-level cache (one-shot, process-lifetime)
    if _TOOL_CACHE is not None:
        return _TOOL_CACHE
    async with _TOOL_CACHE_LOCK:
        if _TOOL_CACHE is not None:  # re-check under lock
            return _TOOL_CACHE
        try:
            _TOOL_CACHE = await _discover_tools_async()
            logger.info("mcp_tools_discovered count=%d", len(_TOOL_CACHE))
        except Exception:
            logger.exception("mcp_tools_discovery_failed")
            _TOOL_CACHE = []
    return _TOOL_CACHE


async def build_mcp_tools_for_agent(agent_id: AgentId) -> Sequence[BaseTool]:
    """Return the curated tool subset for `agent_id`.

    Agents NOT in `_AGENT_TOOL_NAMES` get an empty list — same behavior
    as the legacy `frozenset()` allowlist entries. Adding an agent's
    tools is a code change to `_AGENT_TOOL_NAMES` above.

    Tools are filtered by exact name match against the gateway's
    advertised catalog. If a name in `_AGENT_TOOL_NAMES` doesn't match
    any discovered tool, it's silently skipped — useful when an MCP
    server is temporarily offline (tools missing from the catalog;
    the agent operates with what's available).
    """
    wanted = _AGENT_TOOL_NAMES.get(agent_id, frozenset())
    if not wanted:
        return []
    catalog = await _ensure_tools_loaded_async()
    return [t for t in catalog if t.name in wanted]
