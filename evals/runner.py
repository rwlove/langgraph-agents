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

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import agents.llm as llm_module
from agents.nodes import NODES
from agents.settings import get_settings
from agents.state import FleetState
from evals.registry import is_claude_eligible
from evals.schema import GoldenTask, RunGroup, RunResult

# Per-node wall-clock cap. The nodes are `async def` but call sync-blocking code
# (sync LLM / MCP / file I/O) inside, which pins the event loop — `wait_for`
# alone can't interrupt them. So each node runs in its own worker thread (with
# its own loop for the async body) and `wait_for` bounds it from the main loop.
# On timeout the worker is abandoned (a hung gateway read keeps the thread alive
# but releases the GIL) and the task is recorded as an error rather than
# stalling the whole sweep — ~24min gateway-evidence hangs were observed
# in-cluster. Overridable by the CLI (`--timeout`).
NODE_TIMEOUT_S = 240.0

# Vault subdirs where nodes write their real deliverable. Globbed by task_id —
# unique per golden task, never a live ULID — so capture neither races with nor
# clobbers live fleet traffic.
_DRAFT_DIRS = ("inbox/drafts", "reports/research")

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

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


def _run_node_sync(agent_id: AgentId, state: FleetState) -> dict[str, Any]:
    """Invoke the node to completion in the calling (worker) thread.

    Handles sync and async nodes; an async node gets a fresh event loop here so
    the caller's loop stays free to enforce the timeout. The Claude-forcing
    ``patch.object`` in ``run_task`` is a module-global mutation held across the
    await, so the worker thread sees the patched ``llm``.
    """
    result = NODES[agent_id](state)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


async def _invoke_node(agent_id: AgentId, state: FleetState) -> dict[str, Any]:
    return await asyncio.wait_for(
        asyncio.to_thread(_run_node_sync, agent_id, state), NODE_TIMEOUT_S
    )


def _draft_files_for(task_id: str) -> list[Path]:
    """Vault draft files for one task. The per-task_id glob is exact — golden
    ids never collide with the ULID-named files real fleet traffic produces, so
    this only ever matches files the eval itself wrote."""
    vault_root = get_settings().vault_root
    return [
        p
        for sub in _DRAFT_DIRS
        for p in vault_root.joinpath(sub).glob(f"*{task_id}*.md")
        if p.is_file()
    ]


def _capture_and_clear_draft(task_id: str) -> str:
    """Read back — and remove — the vault file the node just wrote for this task.

    Returns its content, or "" if the node wrote none. Removing it stops the
    eval from polluting the real vault.
    """
    matches = _draft_files_for(task_id)
    if not matches:
        return ""
    newest = max(matches, key=lambda p: p.stat().st_mtime)
    content = newest.read_text(encoding="utf-8", errors="replace")
    for p in matches:
        try:
            p.unlink()
        except OSError:
            pass
    return content


def clear_eval_drafts(task_ids: Iterable[str]) -> int:
    """Sweep-end backstop: remove any leftover vault drafts for these tasks.

    The per-run ``_capture_and_clear_draft`` misses one case — a task that hit
    the node timeout leaves an orphaned worker thread running, which can write
    its draft *after* that cleanup. Those orphans finish during
    ``asyncio.run``'s executor drain, so this must be called from ``main`` once
    that drain completes (not at the end of the async sweep). Returns the count
    removed.
    """
    removed = 0
    for task_id in task_ids:
        for p in _draft_files_for(task_id):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


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
    except TimeoutError:
        _capture_and_clear_draft(task.task_id)  # best-effort; an orphaned worker may still write
        return RunResult(
            agent_id=agent_id,
            task_id=task.task_id,
            group=group,
            latency_s=time.perf_counter() - started,
            error=f"node timeout >{NODE_TIMEOUT_S:.0f}s",
        )
    except Exception as exc:  # the harness records failures, never crashes the sweep
        _capture_and_clear_draft(task.task_id)
        return RunResult(
            agent_id=agent_id,
            task_id=task.task_id,
            group=group,
            latency_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    # Read the real deliverable (and clear it) before the next run overwrites it.
    draft = _capture_and_clear_draft(task.task_id)
    return RunResult(
        agent_id=agent_id,
        task_id=task.task_id,
        group=group,
        output=str(update.get("output") or ""),
        draft=draft,
        latency_s=time.perf_counter() - started,
    )
