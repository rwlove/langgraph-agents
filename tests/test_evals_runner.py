"""Runner: Claude-forcing wrapper + ineligible-skip + patch-seam guard.

The full node-invocation path needs a live cluster (Spark + MCP) and is not
unit-tested here — these cover the cluster-free seams.
"""

from __future__ import annotations

import inspect
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import evals.runner as runner_mod
from agents.nodes import NODES
from agents.state import AgentId
from evals.runner import (
    _capture_and_clear_draft,
    clear_eval_drafts,
    make_claude_wrapper,
    run_task,
)
from evals.schema import GoldenTask, RunResult

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
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


def test_run_result_candidate_prefers_draft_then_output() -> None:
    base = {"agent_id": "ml-operator", "task_id": "t", "group": "local"}
    assert RunResult(**base, output="handle", draft="DEEP").candidate == "DEEP"
    assert RunResult(**base, output="handle").candidate == "handle"  # no draft → handle
    assert RunResult(**base).candidate == ""  # neither


def test_capture_and_clear_draft_reads_then_removes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = tmp_path / "inbox" / "drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "network-net-001-segmentation-audit.md"
    draft.write_text("REAL DELIVERABLE", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "get_settings", lambda: SimpleNamespace(vault_root=tmp_path))

    # globs by task_id (prefix `network-`, agent-specific), reads, and removes it
    assert _capture_and_clear_draft("net-001-segmentation-audit") == "REAL DELIVERABLE"
    assert not draft.exists()  # cleared → no real-vault pollution
    assert _capture_and_clear_draft("net-001-segmentation-audit") == ""  # gone on re-read


def test_capture_and_clear_draft_finds_research_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    research = tmp_path / "reports" / "research"
    research.mkdir(parents=True)
    (research / "res-007-some-slug.md").write_text("FINDINGS", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "get_settings", lambda: SimpleNamespace(vault_root=tmp_path))
    assert _capture_and_clear_draft("res-007") == "FINDINGS"


def test_clear_eval_drafts_removes_leftovers_for_given_tasks(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = tmp_path / "inbox" / "drafts"
    research = tmp_path / "reports" / "research"
    drafts.mkdir(parents=True)
    research.mkdir(parents=True)
    # leftovers an orphaned timed-out worker wrote after its run's own cleanup
    (drafts / "storage-stor-001-tier-audit.md").write_text("orphan", encoding="utf-8")
    (research / "res-007-slug.md").write_text("orphan", encoding="utf-8")
    # a real live-traffic draft (ULID name) the cleanup must NOT touch
    keep = drafts / "observability-01KTZWFNJJYX9QXQ2X3GHJ3JV2.md"
    keep.write_text("live traffic", encoding="utf-8")
    monkeypatch.setattr(runner_mod, "get_settings", lambda: SimpleNamespace(vault_root=tmp_path))

    removed = clear_eval_drafts(["stor-001-tier-audit", "res-007", "never-ran"])

    assert removed == 2
    assert not (drafts / "storage-stor-001-tier-audit.md").exists()
    assert not (research / "res-007-slug.md").exists()
    assert keep.exists()  # live traffic untouched


def test_capture_and_clear_draft_empty_when_no_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "get_settings", lambda: SimpleNamespace(vault_root=tmp_path))
    assert _capture_and_clear_draft("nothing-here") == ""  # missing dirs glob to nothing


async def test_run_task_captures_draft_as_candidate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = tmp_path / "inbox" / "drafts"
    drafts.mkdir(parents=True)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: SimpleNamespace(vault_root=tmp_path))

    def fake_node(state: Any) -> dict[str, str]:
        # mirror the real nodes: write the substance to the vault, return a handle
        (drafts / f"ml-{state.task_id}.md").write_text("FULL ANALYSIS", encoding="utf-8")
        return {"output": "ml finding: /vault/inbox/drafts/ml-ml-001.md (knob=model)"}

    monkeypatch.setitem(NODES, "ml-operator", fake_node)
    res = await run_task("ml-operator", GoldenTask(task_id="ml-001", content="vram math"), "local")

    assert res.error is None
    assert res.output.startswith("ml finding:")  # handle preserved
    assert res.draft == "FULL ANALYSIS"  # real deliverable captured
    assert res.candidate == "FULL ANALYSIS"  # judge will score this, not the handle
    assert not (drafts / "ml-ml-001.md").exists()  # and cleared from the vault


async def test_run_task_times_out_instead_of_stalling(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "get_settings", lambda: SimpleNamespace(vault_root=tmp_path))
    monkeypatch.setattr(runner_mod, "NODE_TIMEOUT_S", 0.1)

    def hung_node(_state: Any) -> dict[str, str]:
        time.sleep(0.5)  # sync-blocking, like a hung gateway evidence call
        return {"output": "never reached"}

    monkeypatch.setitem(NODES, "ml-operator", hung_node)
    res = await run_task("ml-operator", GoldenTask(task_id="ml-timeout", content="x"), "local")

    assert res.error is not None
    assert "timeout" in res.error  # recorded, not a 24min stall


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
