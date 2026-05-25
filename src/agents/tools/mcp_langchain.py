"""Convert MCP allowlist entries into LangChain tool objects.

Bridges the gap between:

- `agents.tools.mcp.MCPGatewayClient` — a thin HTTP client that calls
  the cluster's mcp-gateway over `POST /servers/<server>/tools/<method>`.
- LangChain / LangGraph's ReAct pattern — requires `BaseTool` objects
  the LLM can call iteratively (think → tool_call → observe → reason).

For a given agent_id, this module looks up that agent's
`ALLOWLISTS` entry in `tools/mcp.py`, wraps each allowed
`(server, method)` pair as a `StructuredTool`, and returns the list
ready to be passed to `langgraph.prebuilt.create_react_agent`.

## Why this is the Path 1 unlock

Memory `[[reference_agent_fleet_tool_binding_gap]]` (2026-05-23)
named the gap: every agent except errand-runner uses
`with_structured_output()` over `state.content` — they reason about
a prompt but execute zero tool calls. Operator weekly drift crons
produce LLM reasoning, not data-grounded analysis.

Path 1 of the workaround was "wire ReAct-style tool-binding";
Path 2 (workflow pre-fetches Prom data) shipped first in PR-O.
This module is Path 1 — agents can now call their allowed MCP
methods inside the LLM loop. Started with the `auditor` agent
because its blast radius is the smallest (read-only kubectl;
no MCP writes; no Class C+ actions).

## Schema strategy

For v1, every MCP method is wrapped with a generic
`arguments: dict[str, Any]` signature. The LLM gets a clear
docstring per tool (`"kubectl-mcp/get: kubectl get <resource>
[options]. Pass arguments like {'resource': 'pods', 'namespace':
'ai'}"`) and infers the right shape from training-data familiarity
with kubectl.

A future improvement: introspect the MCP server's `tools/list`
response to generate per-method Pydantic schemas. Skipped for v1
because the LLM does well enough with the generic-dict approach
and per-method schemas double the surface area to maintain.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agents.state import AgentId
from agents.tools.mcp import (
    ALLOWLISTS,
    MCPCallError,
    MCPGatewayClient,
    MCPPermissionError,
)


class _GenericMcpArgs(BaseModel):
    """Generic arguments wrapper for MCP tool calls.

    The LLM passes whatever shape the underlying MCP method expects
    (e.g. ``{"resource": "pods", "namespace": "ai"}`` for
    ``kubectl-mcp/get``). The wrapper passes it through to
    ``MCPGatewayClient.call()`` as-is; the MCP server validates
    its own shape.
    """

    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arguments to pass to the MCP method. Shape varies per "
            "method — see the tool's docstring for examples."
        ),
    )


# Per-MCP-method docstring hints. Each line is the format the LLM
# should see for that tool's `description` field — it should be just
# detailed enough to let the LLM pick the right arguments without
# trial-and-error. If a method isn't listed, falls back to a generic
# "Call <server>.<method>" string.
#
# Doc-strings stay short — qwen2.5:32b's context budget is finite and
# every tool description gets injected into the system prompt.
_METHOD_HINTS: dict[tuple[str, str], str] = {
    # kubectl-mcp — read-only verbs.
    ("kubectl-mcp", "get"): (
        "kubectl get a resource. "
        "Args: {'resource': 'pods'|'deployments'|'pvc'|..., "
        "'namespace'?: str, 'name'?: str, 'output'?: 'json'|'yaml'}."
    ),
    ("kubectl-mcp", "describe"): (
        "kubectl describe a resource. "
        "Args: {'resource': str, 'name': str, 'namespace'?: str}."
    ),
    ("kubectl-mcp", "logs"): (
        "kubectl logs from a pod. "
        "Args: {'pod': str, 'namespace': str, 'container'?: str, "
        "'tail'?: int}."
    ),
    ("kubectl-mcp", "events"): (
        "kubectl get events in a namespace. "
        "Args: {'namespace'?: str}."
    ),
    ("kubectl-mcp", "top"): (
        "kubectl top pods or nodes. "
        "Args: {'resource': 'pods'|'nodes', 'namespace'?: str}."
    ),
    # prometheus-mcp.
    ("prometheus-mcp", "query"): (
        "PromQL instant query. "
        "Args: {'query': str}."
    ),
    ("prometheus-mcp", "query_range"): (
        "PromQL range query. "
        "Args: {'query': str, 'start': int, 'end': int, 'step': str}."
    ),
    ("prometheus-mcp", "alerts"): (
        "List currently-firing Prometheus alerts. Args: {}."
    ),
    # grafana-mcp.
    ("grafana-mcp", "list_dashboards"): (
        "List Grafana dashboards. Args: {}."
    ),
    ("grafana-mcp", "dashboard_by_uid"): (
        "Get a Grafana dashboard JSON by uid. Args: {'uid': str}."
    ),
}


def _build_one_tool(
    agent_id: AgentId,
    server: str,
    method: str,
) -> BaseTool:
    """Wrap a single MCP (server, method) pair as a LangChain BaseTool."""
    name = f"{server}__{method}".replace("-", "_")
    description = _METHOD_HINTS.get(
        (server, method),
        f"Call {server}.{method} on the MCP gateway.",
    )

    def _call(arguments: dict[str, Any] | None = None) -> str:
        """Synchronous MCP call wrapper.

        Returns a string (the LLM's `observation` slot) summarizing
        the MCP response. On error returns a structured error string
        the LLM can reason about — does NOT raise, because that would
        break the ReAct loop. The LLM seeing "PERMISSION_DENIED" or
        "TIMEOUT" is more useful than a Python exception bubbling up.
        """
        try:
            with MCPGatewayClient(agent_id) as client:
                result = client.call(server, method, arguments=arguments or {})
        except MCPPermissionError as exc:
            return f"PERMISSION_DENIED: {exc}"
        except MCPCallError as exc:
            return f"MCP_CALL_ERROR: {exc}"
        except Exception as exc:
            return f"UNEXPECTED_ERROR: {type(exc).__name__}: {exc}"

        # MCPResult.data is whatever the MCP server returned (dict, list,
        # or string). Stringify it consistently so the LLM gets a
        # predictable observation shape. JSON for structured data; cap at
        # ~8K chars to leave room in context.
        data = result.data
        if isinstance(data, (dict, list)):
            text = json.dumps(data, indent=2, default=str)
        else:
            text = str(data)

        if len(text) > 8000:
            text = text[:8000] + f"\n\n... [truncated; {len(text)} chars total]"
        return text

    return StructuredTool.from_function(
        func=_call,
        name=name,
        description=description,
        args_schema=_GenericMcpArgs,
    )


def build_mcp_tools_for_agent(agent_id: AgentId) -> Sequence[BaseTool]:
    """Return LangChain tools for every MCP capability `agent_id` is allowed to call.

    Empty list when the agent has no MCP allowlist (e.g. `triager`,
    `historian`, `reviewer` are vault-only by design).

    The returned tools can be passed directly to
    ``langgraph.prebuilt.create_react_agent(model, tools=...)``.
    """
    capabilities = ALLOWLISTS.get(agent_id, frozenset())
    return [
        _build_one_tool(agent_id, cap.server, cap.method) for cap in capabilities
    ]
