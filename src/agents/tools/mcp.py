"""MCP gateway client with static per-agent scope enforcement.

The mcp-gateway in the cluster exposes 14 MCP servers over HTTP. This module
is the only path agents use to call them. Per-agent allow/deny lists are
defined in code (not in cluster config) so:

  - The boundary is inspectable in PR diffs
  - The "only errand-runner has MCP write" assertion is a static fact
  - Tests can verify the boundary without a running cluster

Per security review cat 2 (reframed 2026-05-15): we use OpenClaw/runtime
tool-permission lists as the per-agent scope mechanism since the Kuadrant
operator is not installed in this cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from agents.settings import get_settings
from agents.state import AgentId


class MCPPermissionError(PermissionError):
    """Raised when an agent attempts an MCP call outside its allowlist."""


class MCPCallError(RuntimeError):
    """Raised when the gateway returns a non-2xx response."""


@dataclass(frozen=True)
class MCPCapability:
    """A (server, method) pair an agent is allowed to invoke.

    `method` is the MCP tool name; `write` distinguishes read-only reads
    from anything that mutates external state. The "no agent except
    errand-runner has write" rule is a static check on this field.
    """

    server: str
    method: str
    write: bool = False


# ---- Per-agent capability allowlists ----
#
# Source of truth for what each agent can call. Adding a new capability is a
# PR change to this file; the test suite asserts errand-runner is the only
# write-capable agent.

_READ_ONLY_KUBECTL: tuple[MCPCapability, ...] = tuple(
    MCPCapability("kubectl-mcp", m, write=False)
    for m in ("get", "describe", "logs", "events", "top")
)

_READ_ONLY_PROMETHEUS: tuple[MCPCapability, ...] = (
    MCPCapability("prometheus-mcp", "query", write=False),
    MCPCapability("prometheus-mcp", "query_range", write=False),
    MCPCapability("prometheus-mcp", "alerts", write=False),
)

_READ_ONLY_GRAFANA: tuple[MCPCapability, ...] = (
    MCPCapability("grafana-mcp", "list_dashboards", write=False),
    MCPCapability("grafana-mcp", "dashboard_by_uid", write=False),
)

_READ_ONLY_HA: tuple[MCPCapability, ...] = (
    MCPCapability("ha-mcp", "states", write=False),
    MCPCapability("ha-mcp", "get_state", write=False),
    MCPCapability("ha-mcp", "history", write=False),
)

_WRITE_HA: tuple[MCPCapability, ...] = (
    MCPCapability("ha-mcp", "call_service", write=True),
    MCPCapability("ha-mcp", "set_state", write=True),
)

_READ_ONLY_PAPERLESS: tuple[MCPCapability, ...] = (
    MCPCapability("paperless-mcp", "search", write=False),
    MCPCapability("paperless-mcp", "documents", write=False),
    MCPCapability("paperless-mcp", "tags", write=False),
)

_WRITE_PAPERLESS: tuple[MCPCapability, ...] = (
    MCPCapability("paperless-mcp", "tag_document", write=True),
    MCPCapability("paperless-mcp", "upload", write=True),
)

_READ_ONLY_NETBOX: tuple[MCPCapability, ...] = (
    MCPCapability("netbox-mcp", "list_devices", write=False),
    MCPCapability("netbox-mcp", "list_ip_addresses", write=False),
    MCPCapability("netbox-mcp", "list_prefixes", write=False),
)

_READ_ONLY_OMADA: tuple[MCPCapability, ...] = (
    MCPCapability("omada-mcp", "list_clients", write=False),
    MCPCapability("omada-mcp", "list_devices", write=False),
)

_READ_ONLY_IMMICH: tuple[MCPCapability, ...] = (
    MCPCapability("immich-mcp", "search_assets", write=False),
    MCPCapability("immich-mcp", "asset_metadata", write=False),
)

_WRITE_ARR: tuple[MCPCapability, ...] = tuple(
    MCPCapability("arr-mcp", m, write=True)
    for m in ("add_movie", "add_series", "add_artist", "delete_movie", "delete_series")
)

_READ_ONLY_SEARXNG: tuple[MCPCapability, ...] = (
    MCPCapability("searxng-mcp", "search", write=False),
)

# ComfyUI image generation — artokun/comfyui-mcp, backed by the ComfyUI on
# the DGX Spark (GB10) at comfyui-spark.ai.svc:8188 (cut over from the P40
# 2026-06-05). Method names are the server's native (un-prefixed) tool names;
# the gateway federates them as `comfyui_*`. The generation tools create GPU
# jobs → write, so they live on errand-runner only (the sole writer). Artist
# holds the read-only subset to size its proposals against live VRAM
# headroom / model inventory / queue depth.
_READ_ONLY_COMFYUI: tuple[MCPCapability, ...] = (
    MCPCapability("comfyui-mcp", "get_system_stats", write=False),
    MCPCapability("comfyui-mcp", "list_local_models", write=False),
    MCPCapability("comfyui-mcp", "list_workflows", write=False),
    MCPCapability("comfyui-mcp", "get_queue", write=False),
    MCPCapability("comfyui-mcp", "get_job_status", write=False),
)

_WRITE_COMFYUI: tuple[MCPCapability, ...] = (
    MCPCapability("comfyui-mcp", "generate_image", write=True),
    MCPCapability("comfyui-mcp", "generate_with_controlnet", write=True),
    MCPCapability("comfyui-mcp", "generate_with_ip_adapter", write=True),
    MCPCapability("comfyui-mcp", "enqueue_workflow", write=True),
)

# Smoke-test pseudo-capability. The server name "smoke" is NOT a real MCP
# server — errand-runner intercepts targets prefixed with "smoke." before
# the MCPGatewayClient call and runs a filesystem self-verifying smoke
# (write → readback → delete) inside the vault smoke-test directory.
#
# Lives in the allowlist so the same gating path (HMAC verify + Class
# check + scope check) exercises the production approval flow end-to-end.
# Triggered via POST /admin/smoke/start-approval.
_SMOKE_TEST: tuple[MCPCapability, ...] = (
    MCPCapability("smoke", "test_write", write=True),
)


# Map agent_id → frozenset of allowed capabilities.
# Adding/removing a capability is an explicit code change reviewable in PR.
ALLOWLISTS: dict[AgentId, frozenset[MCPCapability]] = {
    "triager": frozenset(),  # no MCP — pure classification
    "historian": frozenset(),  # reads vault files, not MCP
    "note-maker": frozenset(_READ_ONLY_SEARXNG + _READ_ONLY_PAPERLESS),
    "researcher": frozenset(_READ_ONLY_SEARXNG + _READ_ONLY_PAPERLESS),
    "coder": frozenset(_READ_ONLY_SEARXNG),
    "errand-runner": frozenset(
        _WRITE_HA
        + _WRITE_PAPERLESS
        + _WRITE_ARR
        + _WRITE_COMFYUI  # executes artist's GenerationRequests under signed approval
        + _READ_ONLY_HA
        + _READ_ONLY_PAPERLESS
        + _READ_ONLY_KUBECTL
        + _READ_ONLY_PROMETHEUS
        + _READ_ONLY_GRAFANA
        + _READ_ONLY_COMFYUI
        + _SMOKE_TEST  # smoke approval-flow verification (filesystem, not MCP)
    ),
    "supervisor": frozenset(
        _READ_ONLY_KUBECTL + _READ_ONLY_PROMETHEUS + _READ_ONLY_GRAFANA
    ),
    "reviewer": frozenset(),  # vault-only, no MCP
    "homelab-engineer": frozenset(
        _READ_ONLY_KUBECTL
        + _READ_ONLY_PROMETHEUS
        + _READ_ONLY_GRAFANA
        + _READ_ONLY_NETBOX
        + _READ_ONLY_OMADA
    ),
    "network-operator": frozenset(
        # L1-L7 network: omada controller + netbox inventory + kubectl
        # network resources + prom/grafana traffic. Read-only - Class C+
        # writes route through errand-runner with signed approval.
        _READ_ONLY_OMADA
        + _READ_ONLY_NETBOX
        + _READ_ONLY_KUBECTL
        + _READ_ONLY_PROMETHEUS
        + _READ_ONLY_GRAFANA
    ),
    "storage-operator": frozenset(
        # PVC / PV / pool / volume / bucket health via kubectl + metrics.
        # No storage-specific MCP yet (no ceph-mcp / longhorn-mcp); CNPG /
        # Barman CR reads are RBAC-denied at the SA level. Class C+ writes
        # route through errand-runner with signed approval.
        _READ_ONLY_KUBECTL + _READ_ONLY_PROMETHEUS + _READ_ONLY_GRAFANA
    ),
    "smart-home-operator": frozenset(
        # HA + protocol hubs + HA-adjacent kubectl reads (for `home` ns
        # pods, music-assistant in `media`, HA's CNPG cluster in
        # `databases`). HA writes route through errand-runner.
        _READ_ONLY_HA
        + _READ_ONLY_KUBECTL
        + _READ_ONLY_PROMETHEUS
        + _READ_ONLY_GRAFANA
        + _READ_ONLY_SEARXNG
        + _READ_ONLY_PAPERLESS
    ),
    "ml-operator": frozenset(
        # Ollama / immich-ml / langgraph-agents pod state via kubectl, GPU
        # + ML metrics via prom/grafana, searxng for model card / vendor
        # research. Pulls / helmrelease bumps / tool toggles route through
        # errand-runner with signed approval.
        _READ_ONLY_KUBECTL + _READ_ONLY_PROMETHEUS + _READ_ONLY_GRAFANA + _READ_ONLY_SEARXNG
    ),
    "observability-operator": frozenset(
        # PrometheusRule / ServiceMonitor / AlertmanagerConfig reads via
        # kubectl. Live Prometheus query + range query (24h replay for
        # flap-testing) via prom. Grafana dashboard inspection. Windmill for
        # the AlertManager→HolmesGPT path. Searxng for upstream rule
        # patterns + Robusta docs. Rule applies / silences / routing
        # changes route through errand-runner.
        _READ_ONLY_KUBECTL
        + _READ_ONLY_PROMETHEUS
        + _READ_ONLY_GRAFANA
        + _READ_ONLY_SEARXNG
    ),
    "health-tracker": frozenset(_READ_ONLY_PAPERLESS),  # ONLY paperless; no external
    "property-coordinator": frozenset(
        _READ_ONLY_PAPERLESS + _READ_ONLY_SEARXNG + _READ_ONLY_IMMICH
    ),
    "doc-writer": frozenset(_READ_ONLY_SEARXNG),  # for verifying upstream terminology
    "reporter": frozenset(),  # output-only — no MCP, no side effects
    # Artist composes GenerationRequests; errand-runner executes the actual
    # comfyui-mcp generation call under signed approval (the write tools live
    # there, not here). Artist gets the read-only ComfyUI subset so it can
    # check live VRAM headroom, the local model inventory, and queue depth
    # before proposing params — the GB10 is shared with ollama-spark, so the
    # available VRAM is dynamic, not fixed.
    "artist": frozenset(_READ_ONLY_COMFYUI),
    # Security reads HA entities (door/lock/motion/away-mode). Frigate access
    # is direct HTTP (not via mcp-gateway) — see SOUL for the why.
    "security": frozenset(_READ_ONLY_HA),
    # Auditor enumerates deployed images (kubectl) + queries GitHub Security
    # Advisories. OSV.dev access is direct HTTP (no community MCP server).
    "auditor": frozenset(_READ_ONLY_KUBECTL),
}


def is_allowed(agent_id: AgentId, server: str, method: str) -> bool:
    """Static check: can `agent_id` invoke `server.method`?"""
    allowlist = ALLOWLISTS.get(agent_id, frozenset())
    return any(c.server == server and c.method == method for c in allowlist)


def agents_with_write_capability() -> set[AgentId]:
    """Used by the test suite to assert errand-runner is the only writer."""
    return {agent_id for agent_id, caps in ALLOWLISTS.items() if any(c.write for c in caps)}


@dataclass(frozen=True)
class MCPResult:
    server: str
    method: str
    status_code: int
    data: dict[str, Any] | list[Any] | str | None


class MCPGatewayClient:
    """Thin HTTP client around the mcp-gateway. One instance per agent.

    Per-agent scope is enforced by `agent_id` passed at construction. The
    `call()` method refuses anything not in the allowlist before any HTTP
    request leaves the pod.
    """

    def __init__(
        self,
        agent_id: AgentId,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.agent_id = agent_id
        self.base_url = (base_url or get_settings().mcp_gateway_url).rstrip("/")
        self._client = httpx.Client(timeout=timeout_seconds)

    def __enter__(self) -> MCPGatewayClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def call(
        self,
        server: str,
        method: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPResult:
        """Invoke an MCP server's tool. Refuses out-of-scope calls statically."""
        if not is_allowed(self.agent_id, server, method):
            raise MCPPermissionError(
                f"agent '{self.agent_id}' is not allowed to call {server}.{method}"
            )

        url = f"{self.base_url}/servers/{server}/tools/{method}"
        try:
            response = self._client.post(url, json={"arguments": arguments or {}})
        except httpx.HTTPError as exc:
            raise MCPCallError(f"transport error calling {server}.{method}: {exc}") from exc

        if response.status_code >= 400:
            raise MCPCallError(
                f"{server}.{method} returned {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError:
            data = response.text

        return MCPResult(
            server=server,
            method=method,
            status_code=response.status_code,
            data=data,
        )
