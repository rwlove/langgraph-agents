"""ml-operator node — LLM mocked; verify the finding renders + writes.

Covers prime-directive shaped fields: recovery_path_touched (Spark-gated
flips, mid-flight pipeline interruption) AND spark_gated (queued for
post-Spark) must round-trip cleanly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from agents.nodes.ml_operator import MLFinding, ml_operator_node
from agents.personas import invalidate_cache, load_persona
from agents.state import ApprovalRequest, FleetState


def _fake_safe_finding() -> MLFinding:
    return MLFinding(
        summary="Bump immich-ml CPU request from 500m to 1000m to cut p95 query latency.",
        failure_domain=(
            "Only immich-ml itself sees the resource bump; no GPU thrash, no "
            "model eviction. Affects immich Search UX during high-traffic "
            "windows."
        ),
        current_baseline=(
            "p95 search latency 1.8s across 24h window (immich_search_duration_seconds "
            "histogram), CPU steady at 480m, memory 1.2 GiB resident."
        ),
        hypothesis=(
            "CPU throttling at 500m limit during search bursts adds ~600ms. "
            "Bumping to 1000m request + 2000m limit removes throttling; expect "
            "p95 to drop to ~900ms."
        ),
        knob="helmrelease_resources",
        knob_change="cpu request 500m → 1000m, limit unchanged at 2000m",
        vram_delta_gib=0.0,
        gpu_headroom_check="N/A — CPU-bound search workload, no GPU resident state.",
        measure_after=(
            "24h after rollout: re-query immich_search_duration_seconds p95 "
            "vs. baseline. Revert if p95 hasn't dropped at least 30%."
        ),
        blast_radius="immich-ml deployment only. No other workload shares this resource bucket.",
        rollback=(
            "# kubernetes/apps/media/immich/app/helmrelease.yaml — revert:\n"
            "  immich-ml:\n"
            "    resources:\n"
            "      requests:\n"
            "        cpu: 500m"
        ),
        recovery_path_touched=False,
        spark_gated=False,
        affects_production=True,
        action_class="C",
        handoff_target="errand-runner",
        affected_resources=["deployment/immich-ml", "media/immich"],
        references=["prom: immich_search_duration_seconds", "reference_gpu_resource_matrix"],
    )


def _fake_spark_gated_finding() -> MLFinding:
    """A Spark-gated decision → forced user handoff + spark_gated true."""
    return MLFinding(
        summary="User asked to flip ENABLE_CLAUDE_API=true on langgraph-agents.",
        failure_domain=(
            "Flipping ENABLE_CLAUDE_API=true at production scale without "
            "Spark backing means specialist agents start consuming Claude "
            "API for inference instead of local Ollama. Spend, latency, "
            "and Anthropic-API rate-limit behavior all shift — none of "
            "which has the band-aid headroom Spark provides."
        ),
        current_baseline=(
            "ENABLE_CLAUDE_API=false in home-ops HelmRelease. Specialists "
            "run against Ollama qwen2.5:7b. Per project_langgraph_activation_gated_on_spark "
            "this gate stays closed until Spark is primary Ollama backend."
        ),
        hypothesis="N/A — DEFERRED until Spark arrival.",
        knob="other",
        knob_change="ENABLE_CLAUDE_API: false → true",
        vram_delta_gib=0.0,
        gpu_headroom_check="N/A — Claude API is off-cluster.",
        measure_after=(
            "Post-Spark: re-evaluate by running fleet workloads against Spark "
            "Ollama for 7d; only flip Claude API on if Spark latency/quality "
            "is insufficient AND budget allows."
        ),
        blast_radius=(
            "All langgraph specialists. Claude API spend potentially substantial; "
            "Anthropic rate-limits unknown at this fleet's volume."
        ),
        rollback=(
            "# kubernetes/apps/home/langgraph-agents/app/helmrelease.yaml — revert:\n"
            "  env:\n"
            "    ENABLE_CLAUDE_API: 'false'"
        ),
        recovery_path_touched=True,
        spark_gated=True,
        affects_production=True,
        action_class="A",
        handoff_target="user",
        affected_resources=["helmrelease/langgraph-agents"],
        references=["project_langgraph_activation_gated_on_spark", "gpu-upgrade-decision"],
    )


async def test_ml_operator_writes_finding_to_vault(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-ml-001",
        source="zulip",
        content="immich search is slow, what can we do about it",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.ml_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.ml_operator.gather_evidence", new=AsyncMock(return_value="")):
        result = await ml_operator_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "ml-t-ml-001.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-ml-001" in body
    assert "kind: ml-finding" in body
    assert "action_class: C" in body
    assert "handoff_target: errand-runner" in body
    assert "spark_gated: False" in body
    assert "recovery_path_touched: False" in body
    assert "knob: helmrelease_resources" in body
    assert "vram_delta_gib: 0.0" in body
    assert "```\n# kubernetes/apps/media/immich" in body
    assert "knob=helmrelease_resources" in result["output"]
    assert "vram_delta=+0.00GiB" in result["output"]
    assert "class=C" in result["output"]
    assert "spark_gated=False" in result["output"]


async def test_ml_operator_surfaces_spark_gated(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-ml-002",
        source="text",
        content="flip on claude api for the agents",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_spark_gated_finding()

    with patch("agents.nodes.ml_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.ml_operator.gather_evidence", new=AsyncMock(return_value="")):
        result = await ml_operator_node(state)

    body = (temp_vault / "inbox" / "drafts" / "ml-t-ml-002.md").read_text()
    assert "spark_gated: True" in body
    assert "recovery_path_touched: True" in body
    assert "⏳ Spark-gated" in body
    assert "⚠️ Recovery path touched" in body
    assert "handoff_target: user" in body
    assert "action_class: A" in body
    assert "spark_gated=True" in result["output"]


def test_ml_finding_schema_rejects_invalid_knob() -> None:
    with pytest.raises(ValidationError):
        MLFinding(
            summary="…",
            failure_domain="…",
            current_baseline="…",
            hypothesis="…",
            knob="bogus_knob",  # type: ignore[arg-type]
            knob_change="…",
            vram_delta_gib=0.0,
            measure_after="…",
            blast_radius="…",
            rollback="…",
            action_class="A",
        )


def test_ml_finding_schema_rejects_invalid_handoff_target() -> None:
    with pytest.raises(ValidationError):
        MLFinding(
            summary="…",
            failure_domain="…",
            current_baseline="…",
            hypothesis="…",
            knob="other",
            knob_change="…",
            vram_delta_gib=0.0,
            measure_after="…",
            blast_radius="…",
            rollback="…",
            action_class="A",
            handoff_target="coder",  # type: ignore[arg-type]
        )


def test_ml_finding_defaults_safe() -> None:
    finding = MLFinding(
        summary="…",
        failure_domain="…",
        current_baseline="…",
        hypothesis="…",
        knob="other",
        knob_change="…",
        vram_delta_gib=0.0,
        measure_after="…",
        blast_radius="…",
        rollback="…",
        action_class="A",
    )
    assert finding.handoff_target == "user"
    assert finding.recovery_path_touched is False
    assert finding.spark_gated is False
    assert finding.affects_production is False
    assert finding.gpu_headroom_check == "N/A"


def test_ml_operator_persona_loads(temp_vault: Path) -> None:
    invalidate_cache()
    persona = load_persona("ml-operator")
    assert persona
    assert "SOUL — ml-operator" in persona


# ---------------------------------------------------------------------------
# ApprovalRequest composition (mirrors PR #39 smart-home-operator wiring).
# ---------------------------------------------------------------------------


async def test_ml_operator_composes_approval_request_for_action(temp_vault: Path) -> None:
    """A Class-C ML finding handed to errand-runner MUST populate
    state.approval_request + target_agent so the fleet graph can route to
    errand-runner and trigger the interrupt() path."""
    state = FleetState(
        task_id="t-ml-action-001",
        source="zulip",
        content="bump immich-ml cpu request to 1000m",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.ml_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.ml_operator.gather_evidence", new=AsyncMock(return_value="")):
        update = await ml_operator_node(state)

    assert update["target_agent"] == "errand-runner"
    req = update["approval_request"]
    assert isinstance(req, ApprovalRequest)
    assert req.action_class == "C"
    assert req.target == "kubectl-mcp.kubectl_apply"
    assert req.proposed_by == "ml-operator"
    # undo_path must be present for Class C — errand-runner refuses Class C
    # without an undo and escalates to D.
    assert req.undo_path is not None
    assert "immich" in req.undo_path
    # payload_summary should describe the knob change.
    assert "helmrelease_resources" in req.payload_summary
    assert "500m" in req.payload_summary


async def test_ml_operator_skips_approval_for_question(temp_vault: Path) -> None:
    """A Class-A (Spark-gated, analysis-only) finding with handoff_target=user
    must NOT set approval_request or target_agent — the existing question/
    research path stays END-only."""
    state = FleetState(
        task_id="t-ml-question-001",
        source="text",
        content="flip on claude api for the agents",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_spark_gated_finding()

    with patch("agents.nodes.ml_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.ml_operator.gather_evidence", new=AsyncMock(return_value="")):
        update = await ml_operator_node(state)

    assert "approval_request" not in update
    assert "target_agent" not in update
