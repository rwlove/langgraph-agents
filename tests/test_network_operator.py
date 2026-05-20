"""network-operator node — LLM is mocked; we verify the finding is rendered + written.

Mirrors the test_note_maker / test_homelab patterns: stub the LLM, invoke the
node, confirm the markdown draft hits the vault and the structured fields
flow through correctly. Adds prime-directive shaped coverage — the
recovery_path_touched flag and the action_class default for the
specialist must round-trip cleanly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents.nodes.network_operator import NetworkFinding, network_operator_node
from agents.personas import invalidate_cache, load_persona
from agents.state import ApprovalRequest, FleetState


def _fake_safe_finding() -> NetworkFinding:
    """A propose-class finding that doesn't touch the recovery path."""
    return NetworkFinding(
        summary=(
            "Camera VLAN 30 needs an ACL allow for the new Reolink "
            "doorbell at 10.30.0.45 to reach Frigate at 10.20.5.12:8554."
        ),
        failure_domain=(
            "Only the new Reolink doorbell loses its RTSP feed to Frigate; "
            "existing cameras unaffected."
        ),
        proposed_change=(
            "Add Omada ACL rule: allow VLAN30 src=10.30.0.45/32 → "
            "VLAN20 dst=10.20.5.12/32 proto=tcp port=8554. "
            "Insert at end of VLAN30 egress rule chain."
        ),
        blast_radius=(
            "Affected: only the new Reolink doorbell (10.30.0.45). "
            "VLAN30 (security) other clients unaffected (no overlap with rule). "
            "VLAN20 (services) sees one new permitted flow."
        ),
        rollback=(
            "delete acl rule id=<assigned-by-controller>\n"
            "verify: omada_listAcls -> rule absent\n"
            "verify reachability still broken: kubectl exec -n media frigate-0 "
            "-- nc -vz 10.30.0.45 8554 -> ECONNREFUSED"
        ),
        recovery_path_touched=False,
        action_class="C",
        handoff_target="errand-runner",
        affected_resources=[
            "VLAN30/security",
            "VLAN20/services",
            "10.30.0.45 (reolink-doorbell)",
            "frigate.media.svc.cluster.local",
        ],
        references=[
            "vault/projects/home-ops/memory/project_reolink_wifi_cameras.md",
            "netbox prefix VLAN30",
        ],
    )


def _fake_recovery_path_finding() -> NetworkFinding:
    """A request that touches the recovery path → forced user handoff."""
    return NetworkFinding(
        summary="User asked to renumber the mgmt VLAN from 99 → 100.",
        failure_domain=(
            "Renumbering mgmt VLAN disconnects iDRAC, switch mgmt, AP mgmt — "
            "potential total fleet-wide control-plane loss until reboot."
        ),
        proposed_change=(
            "DEFERRED — touches the recovery path. See safety protocol. "
            "Class D operation; requires direct user execution after "
            "rollback rehearsal."
        ),
        blast_radius=(
            "All iDRAC, switch-mgmt, AP-mgmt clients on VLAN99 lose "
            "connectivity until reconfigured on VLAN100. Estimated 8+ "
            "control-plane endpoints."
        ),
        rollback=(
            "Revert VLAN-ID change in Omada controller; "
            "force AP-mgmt reconnect via factory-reset if APs lose contact."
        ),
        recovery_path_touched=True,
        action_class="A",
        handoff_target="user",
        affected_resources=["VLAN99/mgmt", "all iDRAC", "all switches", "all APs"],
        references=["IDENTITY.md execution-gate clause 5"],
    )


def test_network_operator_writes_finding_to_vault(temp_vault: Path) -> None:
    """The node writes a draft to inbox/drafts/network-<task_id>.md."""
    state = FleetState(
        task_id="t-net-001",
        source="zulip",
        content=(
            "the new reolink doorbell on 10.30.0.45 can't reach frigate, "
            "what acl change do we need on the security vlan"
        ),
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.network_operator._build_llm", return_value=_FakeLLM()):
        result = network_operator_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "network-t-net-001.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-net-001" in body
    assert "kind: network-finding" in body
    assert "action_class: C" in body
    assert "handoff_target: errand-runner" in body
    assert "recovery_path_touched: False" in body
    assert "Reolink doorbell" in body
    # The verbatim rollback must appear inside a code fence so the user
    # can paste it back without losing structure.
    assert "```\ndelete acl rule" in body
    # Output line carries the routing-relevant fields for the supervisor.
    assert "class=C" in result["output"]
    assert "handoff=errand-runner" in result["output"]
    assert "recovery_path=False" in result["output"]


def test_network_operator_surfaces_recovery_path_touched(temp_vault: Path) -> None:
    """A change touching brain/wg-easy/OOB/DNS/mgmt must surface the warning."""
    state = FleetState(
        task_id="t-net-002",
        source="text",
        content="please renumber the mgmt vlan from 99 to 100",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_recovery_path_finding()

    with patch("agents.nodes.network_operator._build_llm", return_value=_FakeLLM()):
        result = network_operator_node(state)

    body = (temp_vault / "inbox" / "drafts" / "network-t-net-002.md").read_text()
    assert "recovery_path_touched: True" in body
    # The visible warning banner must be present so a human reader can't
    # miss it.
    assert "⚠️ Recovery path touched" in body
    assert "handoff_target: user" in body
    assert "action_class: A" in body
    assert "recovery_path=True" in result["output"]


def test_network_finding_schema_rejects_invalid_action_class() -> None:
    """ActionClass is a Literal — bad values must fail at parse time."""
    with pytest.raises(ValidationError):
        NetworkFinding(
            summary="…",
            failure_domain="…",
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            action_class="X",  # type: ignore[arg-type]
        )


def test_network_finding_schema_rejects_invalid_handoff_target() -> None:
    """handoff_target is Literal[user, errand-runner, homelab-engineer]."""
    with pytest.raises(ValidationError):
        NetworkFinding(
            summary="…",
            failure_domain="…",
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            action_class="A",
            handoff_target="coder",  # type: ignore[arg-type]
        )


def test_network_finding_defaults_safe(temp_vault: Path) -> None:
    """Default handoff is `user`; default recovery_path_touched is False.

    These defaults are load-bearing — an LLM that omits the fields should
    fall through to the safest behavior, not to errand-runner execution.
    """
    finding = NetworkFinding(
        summary="…",
        failure_domain="…",
        proposed_change="…",
        blast_radius="…",
        rollback="…",
        action_class="A",
    )
    assert finding.handoff_target == "user"
    assert finding.recovery_path_touched is False
    assert finding.affected_resources == []
    assert finding.references == []


def test_network_operator_persona_loads(temp_vault: Path) -> None:
    """The persona files in the temp vault must produce a non-empty prompt.

    This is the per-agent equivalent of test_personas_load coverage —
    catches a missing workspace dir at runtime rather than during the
    LLM call.
    """
    invalidate_cache()
    persona = load_persona("network-operator")
    assert persona
    assert "SOUL — network-operator" in persona


# ---------------------------------------------------------------------------
# ApprovalRequest composition (mirrors PR #39 smart-home-operator wiring).
# Off-cluster Omada writes intentionally still route to user / homelab-
# engineer; only cluster-side network changes (CNP, Gateway, HTTPRoute,
# NetworkPolicy, DNSEndpoint) flow through kubectl-mcp.kubectl_apply here.
# ---------------------------------------------------------------------------


def test_network_operator_composes_approval_request_for_action(
    temp_vault: Path,
) -> None:
    """A Class-C network finding handed to errand-runner MUST populate
    state.approval_request + target_agent so the fleet graph can route to
    errand-runner and trigger the interrupt() path."""
    state = FleetState(
        task_id="t-net-action-001",
        source="zulip",
        content="add a cnp egress allow for frigate to reach the new reolink",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.network_operator._build_llm", return_value=_FakeLLM()):
        update = network_operator_node(state)

    assert update["target_agent"] == "errand-runner"
    req = update["approval_request"]
    assert isinstance(req, ApprovalRequest)
    assert req.action_class == "C"
    assert req.target == "kubectl-mcp.kubectl_apply"
    assert req.proposed_by == "network-operator"
    # undo_path must be present for Class C — errand-runner refuses Class C
    # without an undo and escalates to D.
    assert req.undo_path is not None
    assert "delete acl" in req.undo_path
    # payload_summary should describe the proposed change.
    assert "Omada ACL rule" in req.payload_summary or "ACL" in req.payload_summary


def test_network_operator_skips_approval_for_question(temp_vault: Path) -> None:
    """A Class-A (recovery-path, analysis-only) finding with handoff_target=user
    must NOT set approval_request or target_agent — the existing question/
    research path stays END-only."""
    state = FleetState(
        task_id="t-net-question-001",
        source="text",
        content="please renumber the mgmt vlan from 99 to 100",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_recovery_path_finding()

    with patch("agents.nodes.network_operator._build_llm", return_value=_FakeLLM()):
        update = network_operator_node(state)

    assert "approval_request" not in update
    assert "target_agent" not in update
