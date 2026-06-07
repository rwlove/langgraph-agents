"""Runner: Claude-forcing wrapper + ineligible-skip + patch-seam guard.

The full node-invocation path needs a live cluster (Spark + MCP) and is not
unit-tested here — these cover the cluster-free seams.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

from agents.nodes import NODES
from agents.state import AgentId
from evals.runner import make_claude_wrapper, run_task
from evals.schema import GoldenTask

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models.chat_models import BaseChatModel


def test_make_claude_wrapper_injects_group_override() -> None:
    captured: dict[str, Any] = {}

    def fake_llm(agent_id: AgentId, **kwargs: Any) -> str:
        captured["agent_id"] = agent_id
        captured["kwargs"] = kwargs
        return "model-sentinel"

    wrapper = make_claude_wrapper(cast("Callable[..., BaseChatModel]", fake_llm))
    result = wrapper("network-operator", temperature=0.2)

    assert result == "model-sentinel"
    assert captured["agent_id"] == "network-operator"
    assert captured["kwargs"]["group_override"] == "claude"
    assert captured["kwargs"]["temperature"] == 0.2


async def test_run_task_skips_claude_for_ineligible() -> None:
    task = GoldenTask(task_id="h1", content="how did I sleep this week")
    res = await run_task("health-tracker", task, "claude")
    assert res.skipped is True
    assert res.error is None
    assert res.output == ""


def test_operator_node_modules_expose_llm() -> None:
    # The runner forces Claude by patching each node module's `llm` symbol; if a
    # node stopped importing `llm`, the Claude run would error. Guard the seam.
    targets: list[AgentId] = [
        "network-operator",
        "storage-operator",
        "smart-home-operator",
        "ml-operator",
        "observability-operator",
        "homelab-engineer",
        "reporter",
        "researcher",
    ]
    for agent_id in targets:
        module = inspect.getmodule(NODES[agent_id])
        assert module is not None
        assert hasattr(module, "llm"), f"{agent_id} node module has no 'llm' to patch"
