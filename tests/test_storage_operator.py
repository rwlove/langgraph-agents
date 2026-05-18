"""storage-operator node — LLM is mocked; we verify the finding is rendered + written.

Mirrors test_network_operator: stub the LLM, invoke the node, confirm the
markdown draft hits the vault and the structured fields flow through. Adds
prime-directive shaped coverage — the recovery_path_touched flag (Longhorn
NFS backup target / Garage substrate / beast slot-4 OSDs / HA recorder /
in-flight Barman restore) and the action_class default must round-trip.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents.nodes.storage_operator import StorageFinding, storage_operator_node
from agents.personas import invalidate_cache, load_persona
from agents.state import FleetState


def _fake_safe_finding() -> StorageFinding:
    """A propose-class finding that doesn't touch the recovery path."""
    return StorageFinding(
        summary=(
            "New paperless-ngx PVC needed: 50Gi on ceph-block for document "
            "scans + thumbs."
        ),
        failure_domain=(
            "Only the new paperless-ngx workload sees a missing PVC; nothing "
            "existing is affected."
        ),
        proposed_change=(
            "Apply PVC manifest at "
            "kubernetes/apps/collab/paperless-ngx/app/pvc.yaml with "
            "storageClassName: ceph-block, accessModes: [ReadWriteOnce], "
            "resources.requests.storage: 50Gi."
        ),
        backup_recency=(
            "Regenerable: paperless data is also backed by paperless's own "
            "Barman ObjectStore to Garage (existing). New PVC carries no "
            "data at creation; no backup recency check needed pre-apply."
        ),
        blast_radius=(
            "Affected: paperless-ngx (consumer of the new PVC). Ceph pool "
            "current usage 41% — adding 50Gi nominal won't approach min_size "
            "or any pool quota. No other workload references this PVC."
        ),
        capacity_check=(
            "Ceph pool replicapool free: ~3.2 TiB; 50Gi requested; "
            "headroom > 2x check passes."
        ),
        rollback=(
            "kubectl delete pvc -n collab paperless-ngx-data\n"
            "verify: kubectl get pvc -n collab paperless-ngx-data -> NotFound\n"
            "verify Ceph: ceph df -> pool usage unchanged"
        ),
        recovery_path_touched=False,
        action_class="C",
        handoff_target="errand-runner",
        affected_resources=[
            "PVC: collab/paperless-ngx-data",
            "Ceph pool: replicapool",
            "StorageClass: ceph-block",
        ],
        references=[
            ".agents/instructions/storage-class.instructions.md (tier rule 2)",
            "kubernetes/apps/collab/paperless-ngx/app/",
        ],
    )


def _fake_recovery_path_finding() -> StorageFinding:
    """A request that touches the recovery path → forced user handoff."""
    return StorageFinding(
        summary="User asked to reduce HA Barman retention from 7d to 3d.",
        failure_domain=(
            "Reducing HA Barman retention shrinks the rollback window for the "
            "home-assistant recorder cluster. Combined with the recorder's "
            "high write rate, a missed-detection-then-replay window narrows "
            "from 7d to 3d. The 7d cap is intentional per "
            "project_ha_barman_retention_capped."
        ),
        proposed_change=(
            "DEFERRED — touches the HA CNPG cluster (recorder write path). "
            "Class D operation; requires direct user execution after weighing "
            "the storage-vs-recovery-window tradeoff that landed on 7d "
            "originally."
        ),
        backup_recency=(
            "Last Barman base backup recency unobservable from mcp-kubectl SA "
            "(CR reads RBAC-denied). User-side check needed: "
            "`kubectl get backups.postgresql.cnpg.io -n databases`."
        ),
        blast_radius=(
            "HA recorder rollback window narrows from 7d → 3d. Affects any "
            "future incident requiring replay-from-backup older than 3d. No "
            "other CNPG clusters affected (this is a per-ObjectStore setting)."
        ),
        capacity_check=(
            "N/A — retention reduction frees space, doesn't grow footprint."
        ),
        rollback=(
            "Revert the ObjectStore CR retention field from 3d back to 7d:\n"
            "  spec.retentionPolicy: '7d'\n"
            "Re-apply via Flux or kubectl apply."
        ),
        recovery_path_touched=True,
        action_class="A",
        handoff_target="user",
        affected_resources=[
            "ObjectStore: databases/home-assistant-backup",
            "CNPG cluster: databases/home-assistant",
        ],
        references=["project_ha_barman_retention_capped"],
    )


def test_storage_operator_writes_finding_to_vault(temp_vault: Path) -> None:
    """The node writes a draft to inbox/drafts/storage-<task_id>.md."""
    state = FleetState(
        task_id="t-stg-001",
        source="zulip",
        content="add a 50Gi pvc for paperless-ngx on ceph-block",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.storage_operator._build_llm", return_value=_FakeLLM()):
        result = storage_operator_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "storage-t-stg-001.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-stg-001" in body
    assert "kind: storage-finding" in body
    assert "action_class: C" in body
    assert "handoff_target: errand-runner" in body
    assert "recovery_path_touched: False" in body
    assert "paperless-ngx" in body
    # Verbatim rollback must be inside a code fence.
    assert "```\nkubectl delete pvc" in body
    # Output line carries routing-relevant fields.
    assert "class=C" in result["output"]
    assert "handoff=errand-runner" in result["output"]
    assert "recovery_path=False" in result["output"]


def test_storage_operator_surfaces_recovery_path_touched(temp_vault: Path) -> None:
    """A change touching HA recorder / NFS backup target / beast slot-4 / Garage
    substrate / in-flight Barman restore must surface the warning."""
    state = FleetState(
        task_id="t-stg-002",
        source="text",
        content="please reduce ha barman retention from 7d to 3d to save garage space",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_recovery_path_finding()

    with patch("agents.nodes.storage_operator._build_llm", return_value=_FakeLLM()):
        result = storage_operator_node(state)

    body = (temp_vault / "inbox" / "drafts" / "storage-t-stg-002.md").read_text()
    assert "recovery_path_touched: True" in body
    assert "⚠️ Recovery path touched" in body
    assert "handoff_target: user" in body
    assert "action_class: A" in body
    assert "recovery_path=True" in result["output"]


def test_storage_finding_schema_rejects_invalid_action_class() -> None:
    """ActionClass is a Literal — bad values must fail at parse time."""
    with pytest.raises(ValidationError):
        StorageFinding(
            summary="…",
            failure_domain="…",
            proposed_change="…",
            backup_recency="…",
            blast_radius="…",
            rollback="…",
            action_class="X",  # type: ignore[arg-type]
        )


def test_storage_finding_schema_rejects_invalid_handoff_target() -> None:
    """handoff_target is Literal[user, errand-runner, homelab-engineer,
    smart-home-operator, ml-operator]."""
    with pytest.raises(ValidationError):
        StorageFinding(
            summary="…",
            failure_domain="…",
            proposed_change="…",
            backup_recency="…",
            blast_radius="…",
            rollback="…",
            action_class="A",
            handoff_target="coder",  # type: ignore[arg-type]
        )


def test_storage_finding_defaults_safe(temp_vault: Path) -> None:
    """Default handoff is `user`; default recovery_path_touched is False;
    default capacity_check is 'N/A'.

    These defaults are load-bearing — an LLM that omits the fields should
    fall through to the safest behavior, not to errand-runner execution.
    """
    finding = StorageFinding(
        summary="…",
        failure_domain="…",
        proposed_change="…",
        backup_recency="…",
        blast_radius="…",
        rollback="…",
        action_class="A",
    )
    assert finding.handoff_target == "user"
    assert finding.recovery_path_touched is False
    assert finding.capacity_check == "N/A"
    assert finding.affected_resources == []
    assert finding.references == []


def test_storage_operator_persona_loads(temp_vault: Path) -> None:
    """The persona files in the temp vault must produce a non-empty prompt."""
    invalidate_cache()
    persona = load_persona("storage-operator")
    assert persona
    assert "SOUL — storage-operator" in persona
