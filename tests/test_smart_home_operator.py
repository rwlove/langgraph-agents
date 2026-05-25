"""smart-home-operator node — LLM mocked; verify the finding renders + writes.

Covers the prime-directive shaped fields: recovery_path_touched (safety
devices, HA recorder, core integration disable) AND sleep_hours_warning
(automation that could fire 00:00-06:00) must round-trip cleanly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from agents.nodes.smart_home_operator import SmartHomeFinding, smart_home_operator_node
from agents.personas import invalidate_cache, load_persona
from agents.state import ApprovalRequest, FleetState


def _fake_safe_finding() -> SmartHomeFinding:
    return SmartHomeFinding(
        summary=(
            "Porch light automation isn't triggering on motion at dusk — "
            "trigger uses elevation < 5 but motion sensor entity_id changed."
        ),
        failure_domain=(
            "Only the porch-light dusk automation fails; no other automation "
            "or safety device affected."
        ),
        entities=["light.porch", "binary_sensor.porch_motion", "sun.sun"],
        devices=[],
        diagnosis=(
            "ha_get_automation_traces shows last run was 2026-04-02; "
            "binary_sensor.porch_motion was renamed to "
            "binary_sensor.porch_motion_v2 when the Z-Wave node was rejoined."
        ),
        proposed_change=(
            "Update automation 'porch_light_at_dusk' trigger entity_id from "
            "binary_sensor.porch_motion to binary_sensor.porch_motion_v2 in "
            "packages/lighting.yaml."
        ),
        config_validated=(
            "ha_check_config: OK. ha_eval_template against the new entity "
            "returned 'on' for current sensor state."
        ),
        blast_radius=(
            "Only the porch_light_at_dusk automation references "
            "binary_sensor.porch_motion. No scenes, scripts, or dashboards "
            "reference the old entity_id (grep -r porch_motion confirmed)."
        ),
        rollback=(
            "# old automation trigger (revert in packages/lighting.yaml):\n"
            "  trigger:\n"
            "    - platform: state\n"
            "      entity_id: binary_sensor.porch_motion\n"
            "      to: 'on'"
        ),
        recovery_path_touched=False,
        sleep_hours_warning=False,
        action_class="C",
        handoff_target="errand-runner",
        affected_resources=[
            "automation.porch_light_at_dusk",
            "packages/lighting.yaml",
        ],
        references=["ha_get_automation_traces", "ha-config-sync.md"],
    )


def _fake_safety_device_finding() -> SmartHomeFinding:
    """A request that touches a safety device → forced user handoff."""
    return SmartHomeFinding(
        summary="User asked to add auto-unlock to front door on arrival.",
        failure_domain=(
            "Auto-unlock on a door lock is a safety-relevant operation. "
            "False positive geofence triggers (e.g., GPS jump while phone "
            "indoors) could unlock the front door when no one's home."
        ),
        entities=["lock.front_door", "device_tracker.rob_phone"],
        devices=["zwave-node-12"],
        diagnosis="N/A — design review only.",
        proposed_change=(
            "DEFERRED — locks are in the always-propose list. Recommend "
            "user implement manually with high-confidence presence guard "
            "(home Wi-Fi connected + device_tracker.home AND GPS within "
            "30m for 60s+), or skip auto-unlock entirely."
        ),
        config_validated="N/A — proposed yaml not authored.",
        blast_radius=(
            "Front door unlock affects house physical security. No other "
            "automation currently controls lock.front_door."
        ),
        rollback=(
            "If implemented and regretted: delete the automation, set "
            "lock.front_door state back to locked manually."
        ),
        recovery_path_touched=True,
        sleep_hours_warning=False,
        action_class="A",
        handoff_target="user",
        affected_resources=["lock.front_door", "automation.front_door_auto_unlock"],
        references=["always-propose list: lock.unlock"],
    )


async def test_smart_home_operator_writes_finding_to_vault(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-sh-001",
        source="zulip",
        content="the porch light isn't coming on at dusk anymore",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.smart_home_operator.gather_evidence", new=AsyncMock(return_value="")):
        result = await smart_home_operator_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "smart-home-t-sh-001.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-sh-001" in body
    assert "kind: smart-home-finding" in body
    assert "action_class: C" in body
    assert "handoff_target: errand-runner" in body
    assert "recovery_path_touched: False" in body
    assert "sleep_hours_warning: False" in body
    assert "binary_sensor.porch_motion_v2" in body
    assert "```\n# old automation trigger" in body
    assert "class=C" in result["output"]
    assert "handoff=errand-runner" in result["output"]


async def test_smart_home_operator_surfaces_safety_device_touched(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-sh-002",
        source="text",
        content="add auto-unlock to the front door when i arrive home",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safety_device_finding()

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.smart_home_operator.gather_evidence", new=AsyncMock(return_value="")):
        result = await smart_home_operator_node(state)

    body = (temp_vault / "inbox" / "drafts" / "smart-home-t-sh-002.md").read_text()
    assert "recovery_path_touched: True" in body
    assert "⚠️ Recovery path touched" in body
    assert "handoff_target: user" in body
    assert "action_class: A" in body
    assert "recovery_path=True" in result["output"]


def test_smart_home_finding_schema_rejects_invalid_handoff_target() -> None:
    with pytest.raises(ValidationError):
        SmartHomeFinding(
            summary="…",
            failure_domain="…",
            diagnosis="…",
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            action_class="A",
            handoff_target="coder",  # type: ignore[arg-type]
        )


def test_smart_home_finding_defaults_safe() -> None:
    """Default handoff is `user`; flags default False; entities/devices empty."""
    finding = SmartHomeFinding(
        summary="…",
        failure_domain="…",
        diagnosis="…",
        proposed_change="…",
        blast_radius="…",
        rollback="…",
        action_class="A",
    )
    assert finding.handoff_target == "user"
    assert finding.recovery_path_touched is False
    assert finding.sleep_hours_warning is False
    assert finding.entities == []
    assert finding.devices == []
    assert finding.config_validated == "N/A"


def test_smart_home_operator_persona_loads(temp_vault: Path) -> None:
    invalidate_cache()
    persona = load_persona("smart-home-operator")
    assert persona
    assert "SOUL — smart-home-operator" in persona


# ---------------------------------------------------------------------------
# ApprovalRequest composition (demonstrates the propose-then-execute pattern
# wired to errand-runner's interrupt() path from PR #36).
# ---------------------------------------------------------------------------


def _fake_action_finding() -> SmartHomeFinding:
    """A class-C HA write that hands off to errand-runner — the canonical
    propose-then-execute case (toggle a light, no safety device touched)."""
    return SmartHomeFinding(
        summary="User asked to turn off the porch light.",
        failure_domain="Only porch light state; no automation depends on it being on.",
        entities=["light.porch"],
        devices=[],
        diagnosis="Trivial service call — light.turn_off on light.porch.",
        proposed_change=("Call light.turn_off on light.porch via ha-mcp.call_service."),
        config_validated="N/A — not a config change.",
        blast_radius=("Only light.porch. No automations reference its state directly."),
        rollback="light.turn_on on light.porch (single inverse service call).",
        recovery_path_touched=False,
        sleep_hours_warning=False,
        action_class="C",
        handoff_target="errand-runner",
        affected_resources=["light.porch"],
        references=["ha_call_service"],
    )


async def test_smart_home_operator_composes_approval_request_for_action(
    temp_vault: Path,
) -> None:
    """A Class-C finding handed to errand-runner MUST populate
    state.approval_request + target_agent so the fleet graph can route to
    errand-runner and trigger the interrupt() path."""
    state = FleetState(
        task_id="t-sh-action-001",
        source="zulip",
        content="turn off the porch light",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_action_finding()

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.smart_home_operator.gather_evidence", new=AsyncMock(return_value="")):
        update = await smart_home_operator_node(state)

    assert update["target_agent"] == "errand-runner"
    req = update["approval_request"]
    assert isinstance(req, ApprovalRequest)
    assert req.action_class == "C"
    assert req.target == "ha-mcp.call_service"
    assert req.proposed_by == "smart-home-operator"
    # undo_path must be present for Class C — errand-runner refuses Class C
    # without an undo and escalates to D.
    assert req.undo_path is not None
    assert "light.turn_on" in req.undo_path
    assert "light.porch" in req.payload_summary
    assert "light.turn_off" in req.payload_summary


async def test_smart_home_operator_skips_approval_for_question(temp_vault: Path) -> None:
    """A Class-A (analysis-only) finding with handoff_target=user must NOT
    set approval_request or target_agent — the existing question/research
    path stays END-only."""
    finding = SmartHomeFinding(
        summary="Why didn't the porch light come on?",
        failure_domain="Analysis only.",
        diagnosis="Likely renamed entity_id; needs investigation.",
        proposed_change="Investigate entity rename in ha_get_automation_traces.",
        blast_radius="None — analysis only.",
        rollback="N/A — no change proposed.",
        action_class="A",
        handoff_target="user",
    )
    state = FleetState(
        task_id="t-sh-question-001",
        source="zulip",
        content="why didn't the porch light come on at dusk?",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return finding

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.smart_home_operator.gather_evidence", new=AsyncMock(return_value="")):
        update = await smart_home_operator_node(state)

    assert "approval_request" not in update
    assert "target_agent" not in update


async def test_smart_home_operator_skips_approval_for_class_b_handoff(
    temp_vault: Path,
) -> None:
    """A Class-B vault-draft handoff (handoff_target=errand-runner is
    technically possible but the eight-clause gate keeps writes at C). If a
    LLM emits B + errand-runner anyway, we still skip approval — errand-runner
    isn't the broker for vault drafts."""
    finding = SmartHomeFinding(
        summary="Draft a new automation for the porch light.",
        failure_domain="Draft only.",
        diagnosis="No change yet.",
        proposed_change="Write a draft automation in the vault.",
        blast_radius="None — vault-local.",
        rollback="Delete the draft.",
        action_class="B",
        handoff_target="errand-runner",
    )
    state = FleetState(
        task_id="t-sh-draft-001",
        source="text",
        content="draft a porch automation",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return finding

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()), \
         patch("agents.nodes.smart_home_operator.gather_evidence", new=AsyncMock(return_value="")):
        update = await smart_home_operator_node(state)

    assert "approval_request" not in update
    assert "target_agent" not in update
