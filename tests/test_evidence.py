"""gather_evidence — recursion-limit salvage + summary extraction.

Regression coverage for langgraph-agents#130: a ReAct evidence loop that trips
the recursion limit must salvage the tool observations it already gathered
instead of returning an empty block (which made the caller synthesize blind and
the local model fabricate).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from agents.nodes import _evidence


class _FakeAgent:
    """Stands in for a create_react_agent runnable: astream yields states, then
    optionally raises (to simulate the recursion-limit abort)."""

    def __init__(self, states: list[dict], raise_exc: Exception | None = None) -> None:
        self._states = states
        self._raise = raise_exc

    async def astream(self, _input, _config, stream_mode="values"):
        for state in self._states:
            yield state
        if self._raise is not None:
            raise self._raise


def _patch_env(agent: _FakeAgent):
    return (
        patch.object(
            _evidence, "build_mcp_tools_for_agent", new=AsyncMock(return_value=[object()])
        ),
        patch.object(_evidence, "llm", return_value=object()),
        patch.object(
            _evidence,
            "get_settings",
            return_value=SimpleNamespace(
                grafana_prometheus_datasource_uid="prom-uid",
                grafana_loki_datasource_uid="loki-uid",
            ),
        ),
        patch.object(_evidence, "create_react_agent", return_value=agent),
    )


async def test_no_tools_returns_empty() -> None:
    with patch.object(_evidence, "build_mcp_tools_for_agent", new=AsyncMock(return_value=[])):
        assert await _evidence.gather_evidence("storage-operator", "req") == ""


async def test_happy_path_returns_final_summary() -> None:
    final = AIMessage(content="- prometheus_query: 3 OSDs up, 0 down")
    agent = _FakeAgent(states=[{"messages": [HumanMessage(content="x"), final]}])
    p1, p2, p3, p4 = _patch_env(agent)
    with p1, p2, p3, p4:
        out = await _evidence.gather_evidence("storage-operator", "req")
    assert out == "- prometheus_query: 3 OSDs up, 0 down"


async def test_recursion_limit_salvages_tool_observations() -> None:
    tool_msg = ToolMessage(content="osd.4 is down", name="prometheus_query", tool_call_id="c1")
    pending = AIMessage(content="", tool_calls=[{"name": "loki_query", "args": {}, "id": "c2"}])
    agent = _FakeAgent(
        states=[{"messages": [HumanMessage(content="x"), tool_msg, pending]}],
        raise_exc=GraphRecursionError("recursion limit reached"),
    )
    p1, p2, p3, p4 = _patch_env(agent)
    with p1, p2, p3, p4:
        out = await _evidence.gather_evidence("storage-operator", "req")
    # Previously returned "" — must now carry the gathered observation.
    assert out == "- prometheus_query: osd.4 is down"


async def test_non_recursion_exception_returns_empty() -> None:
    agent = _FakeAgent(states=[], raise_exc=RuntimeError("gateway down"))
    p1, p2, p3, p4 = _patch_env(agent)
    with p1, p2, p3, p4:
        assert await _evidence.gather_evidence("storage-operator", "req") == ""
