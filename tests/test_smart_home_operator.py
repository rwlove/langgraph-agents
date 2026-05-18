"""smart-home-operator node — LLM mocked; verify the finding renders + writes.

Covers the prime-directive shaped fields: recovery_path_touched (safety
devices, HA recorder, core integration disable) AND sleep_hours_warning
(automation that could fire 00:00-06:00) must round-trip cleanly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents.nodes.smart_home_operator import SmartHomeFinding, smart_home_operator_node
from agents.personas import invalidate_cache, load_persona
from agents.state import FleetState


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


def test_smart_home_operator_writes_finding_to_vault(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-sh-001",
        source="zulip",
        content="the porch light isn't coming on at dusk anymore",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()):
        result = smart_home_operator_node(state)

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


def test_smart_home_operator_surfaces_safety_device_touched(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-sh-002",
        source="text",
        content="add auto-unlock to the front door when i arrive home",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safety_device_finding()

    with patch("agents.nodes.smart_home_operator._build_llm", return_value=_FakeLLM()):
        result = smart_home_operator_node(state)

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
