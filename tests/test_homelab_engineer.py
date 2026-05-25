"""homelab-engineer node — LLM mocked; verify the finding renders + writes.

Mirrors the test_network_operator / test_storage_operator patterns: stub
the LLM, invoke the node, confirm the markdown draft hits the vault and
the structured fields flow through. Adds the ApprovalRequest-composition
coverage that PR #39 introduced for smart-home-operator.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents.nodes.homelab_engineer import HomelabFinding, homelab_engineer_node
from agents.personas import invalidate_cache, load_persona
from agents.state import ApprovalRequest, FleetState


def _fake_safe_finding() -> HomelabFinding:
    """A Class-C cluster change handed to errand-runner — the canonical
    propose-then-execute case (bump a single HelmRelease resource)."""
    return HomelabFinding(
        summary=(
            "kube-prometheus-stack alertmanager pod is OOMKilling every ~4h "
            "under current 256Mi limit; bump memory limit to 512Mi."
        ),
        diagnosis=(
            "kubectl top + prom histogram show steady-state RSS ~210Mi with "
            "spikes to 300Mi during alert-storm windows. 256Mi limit is "
            "below the steady-state ceiling. No SPOF — alertmanager has 3 "
            "replicas, but the pattern hits all of them."
        ),
        proposed_action=(
            "Edit kubernetes/apps/observability/kube-prometheus-stack/app/"
            "helmrelease.yaml: alertmanager.resources.limits.memory 256Mi → "
            "512Mi (requests unchanged)."
        ),
        action_class="C",
        handoff_target="errand-runner",
        target_repo="home-ops",
        affected_resources=[
            "helmrelease/kube-prometheus-stack",
            "statefulset/alertmanager-kube-prometheus-stack-alertmanager",
        ],
        references=[
            'prom: container_memory_working_set_bytes{pod=~"alertmanager-.*"}',
            "kubernetes/apps/observability/kube-prometheus-stack/app/helmrelease.yaml",
        ],
        rollback=(
            "# kubernetes/apps/observability/kube-prometheus-stack/app/helmrelease.yaml — revert:\n"
            "  alertmanager:\n"
            "    resources:\n"
            "      limits:\n"
            "        memory: 256Mi"
        ),
    )


def _fake_question_finding() -> HomelabFinding:
    """A Class-A analysis-only finding with handoff_target=user (the
    question / status-check path)."""
    return HomelabFinding(
        summary="ceph-block free capacity report for the next 30 days.",
        diagnosis=(
            "Current usage 41%; growth rate ~1.2 GiB/d over trailing 30d. "
            "No projected pressure inside the 30d window."
        ),
        proposed_action=(
            "Analysis only; no action needed. Re-run if growth rate "
            "doubles or if a large new workload (>200 GiB) gets scheduled."
        ),
        action_class="A",
        handoff_target="user",
        target_repo="home-ops",
        affected_resources=["Ceph pool: replicapool"],
        references=["prom: ceph_pool_stored_raw / ceph_pool_max_avail"],
    )


async def test_homelab_engineer_writes_finding_to_vault(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-hl-001",
        source="zulip",
        content="alertmanager keeps oomkilling, please look at it",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.homelab_engineer._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.homelab_engineer.gather_evidence", new=AsyncMock(return_value="")):
        result = await homelab_engineer_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "homelab-t-hl-001.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-hl-001" in body
    assert "kind: homelab-finding" in body
    assert "action_class: C" in body
    assert "handoff_target: errand-runner" in body
    assert "target_repo: home-ops" in body
    assert "alertmanager" in body
    # Verbatim rollback must be inside a code fence when present.
    assert "```\n# kubernetes/apps/observability" in body
    assert "class=C" in result["output"]
    assert "handoff=errand-runner" in result["output"]


async def test_homelab_engineer_renders_without_rollback_for_question(
    temp_vault: Path,
) -> None:
    """A Class-A analysis-only finding has no rollback (default empty).
    The markdown skips the rollback section entirely — no empty code fence."""
    state = FleetState(
        task_id="t-hl-002",
        source="text",
        content="what's our ceph free capacity look like",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_question_finding()

    with patch("agents.nodes.homelab_engineer._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.homelab_engineer.gather_evidence", new=AsyncMock(return_value="")):
        await homelab_engineer_node(state)

    body = (temp_vault / "inbox" / "drafts" / "homelab-t-hl-002.md").read_text()
    # No rollback section when finding.rollback is empty (no empty code fence).
    assert "Rollback" not in body
    assert "```\n```" not in body


def test_homelab_finding_defaults_safe() -> None:
    """Default handoff is `user`, default rollback is empty string — the
    safe behavior when an LLM omits the fields."""
    finding = HomelabFinding(
        summary="…",
        diagnosis="…",
        proposed_action="…",
        action_class="A",
    )
    assert finding.handoff_target == "user"
    assert finding.target_repo == "home-ops"
    assert finding.rollback == ""
    assert finding.affected_resources == []


def test_homelab_engineer_persona_loads(temp_vault: Path) -> None:
    invalidate_cache()
    persona = load_persona("homelab-engineer")
    assert persona
    assert "SOUL — homelab-engineer" in persona


# ---------------------------------------------------------------------------
# ApprovalRequest composition (mirrors PR #39 smart-home-operator wiring).
# ---------------------------------------------------------------------------


async def test_homelab_engineer_composes_approval_request_for_action(
    temp_vault: Path,
) -> None:
    """A Class-C homelab finding handed to errand-runner MUST populate
    state.approval_request + target_agent so the fleet graph can route to
    errand-runner and trigger the interrupt() path."""
    state = FleetState(
        task_id="t-hl-action-001",
        source="zulip",
        content="bump alertmanager memory limit",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.homelab_engineer._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.homelab_engineer.gather_evidence", new=AsyncMock(return_value="")):
        update = await homelab_engineer_node(state)

    assert update["target_agent"] == "errand-runner"
    req = update["approval_request"]
    assert isinstance(req, ApprovalRequest)
    assert req.action_class == "C"
    assert req.target == "kubectl-mcp.kubectl_apply"
    assert req.proposed_by == "homelab-engineer"
    # undo_path must be present for Class C — errand-runner refuses Class C
    # without an undo and escalates to D.
    assert req.undo_path is not None
    assert "kube-prometheus-stack" in req.undo_path or "alertmanager" in req.undo_path
    # payload_summary should describe the proposed action.
    assert "helmrelease" in req.payload_summary.lower() or "alertmanager" in req.payload_summary


async def test_homelab_engineer_skips_approval_for_question(temp_vault: Path) -> None:
    """A Class-A analysis-only finding with handoff_target=user must NOT
    set approval_request or target_agent — the existing question/research
    path stays END-only."""
    state = FleetState(
        task_id="t-hl-question-001",
        source="text",
        content="what's our ceph free capacity look like",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_question_finding()

    with patch("agents.nodes.homelab_engineer._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.homelab_engineer.gather_evidence", new=AsyncMock(return_value="")):
        update = await homelab_engineer_node(state)

    assert "approval_request" not in update
    assert "target_agent" not in update
