"""Run a golden task through an agent's real node, swapping only the model.

The faithful-comparison trick: invoke the *actual* node (same persona, same
evidence pre-fetch, same structured-output schema) and force only the synthesis
model to Claude by monkeypatching the node module's `llm` symbol. Evidence
gathering lives in ``agents.nodes._evidence`` with its own `llm` binding, so it
stays on its default backend across both runs — we measure the synthesis model,
not the evidence model.

Claude is forced via ``group_override="claude"`` rather than ``escalate=True``
on purpose: ``llm()`` *raises* if Claude isn't allowed (ENABLE_CLAUDE_API off /
no key), so a misconfigured run fails loudly instead of silently degrading to
local and corrupting the A/B.
"""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import agents.llm as llm_module
from agents.nodes import NODES
from agents.state import FleetState
from evals.registry import is_claude_eligible
from evals.schema import GoldenTask, RunGroup, RunResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models.chat_models import BaseChatModel

    from agents.state import AgentId


def make_claude_wrapper(
    real_llm: Callable[..., BaseChatModel],
) -> Callable[..., BaseChatModel]:
    """Wrap ``agents.llm.llm`` to force the Claude group for the synthesis call.

    ``health-tracker`` is downgraded back to local inside ``llm()`` by its hard
    pin even with the override — but the runner never wraps for ineligible
    agents (the Claude run is skipped), so that path isn't exercised here.
    """

    def wrapper(agent_id: AgentId, **kwargs: Any) -> BaseChatModel:
        kwargs["group_override"] = "claude"
        return real_llm(agent_id, **kwargs)

    return wrapper


def _state_for(agent_id: AgentId, task: GoldenTask) -> FleetState:
    return FleetState(
        task_id=task.task_id,
        source="test",
        content=task.content,
        data_tier=task.data_tier,
        target_agent=agent_id,
    )


async def _invoke_node(agent_id: AgentId, state: FleetState) -> dict[str, Any]:
    result = NODES[agent_id](state)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_task(agent_id: AgentId, task: GoldenTask, group: RunGroup) -> RunResult:
    """Run one task through `agent_id`'s node on the given backend.

    Never raises — node failures are captured into ``RunResult.error`` so a
    single bad task can't abort a sweep.
    """
    if group == "claude" and not is_claude_eligible(agent_id):
        return RunResult(agent_id=agent_id, task_id=task.task_id, group=group, skipped=True)

    # Resolve the Claude-forcing patch before timing, so a wiring problem
    # (node with no `llm` symbol) is reported, not silently run on local.
    patch_ctx = None
    if group == "claude":
        module = inspect.getmodule(NODES[agent_id])
        if module is None or not hasattr(module, "llm"):
            return RunResult(
                agent_id=agent_id,
                task_id=task.task_id,
                group=group,
                error="node module exposes no 'llm' symbol to force the Claude group",
            )
        patch_ctx = patch.object(module, "llm", make_claude_wrapper(llm_module.llm))

    state = _state_for(agent_id, task)
    started = time.perf_counter()
    try:
        if patch_ctx is not None:
            with patch_ctx:
                update = await _invoke_node(agent_id, state)
        else:
            update = await _invoke_node(agent_id, state)
    except Exception as exc:  # the harness records failures, never crashes the sweep
        return RunResult(
            agent_id=agent_id,
            task_id=task.task_id,
            group=group,
            latency_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    return RunResult(
        agent_id=agent_id,
        task_id=task.task_id,
        group=group,
        output=str(update.get("output") or ""),
        latency_s=time.perf_counter() - started,
    )
