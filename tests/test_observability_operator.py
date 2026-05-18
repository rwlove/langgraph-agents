"""observability-operator node — LLM mocked; verify the finding renders + writes.

Covers prime-directive shaped fields: recovery_path_touched (receiver disable,
class silencing, retention reduction, ruleless removal) must round-trip cleanly.
Also asserts that flood_mode AND mute_mode are required — the discipline this
role enforces is naming BOTH failure directions on every proposal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents.nodes.observability_operator import ObservabilityFinding, observability_operator_node
from agents.personas import invalidate_cache, load_persona
from agents.state import FleetState


def _fake_safe_finding() -> ObservabilityFinding:
    """A propose-class additive rule with proper flap protection."""
    return ObservabilityFinding(
        summary=(
            "Add PrometheusRule alerting on Longhorn volume degraded state — "
            "currently no rule fires until the volume goes Faulted (too late)."
        ),
        flood_mode=(
            "Longhorn occasionally marks volumes Degraded transiently during "
            "replica rebuild after a node restart. Without a `for:` clause "
            "this would page on every node reboot. With for=10m it should "
            "only fire on real degradation lasting past the rebuild window."
        ),
        mute_mode=(
            "If the threshold is set too tight (for too long), we miss the "
            "early warning before Faulted. 10m is the safe midpoint per "
            "24h replay — typical rebuild completes in <8m."
        ),
        proposed_change=(
            "Add PrometheusRule in kubernetes/apps/storage/longhorn/:\n"
            "  alert: LonghornVolumeDegraded\n"
            "  expr: longhorn_volume_state == 3   # 3 = Degraded\n"
            "  for: 10m\n"
            "  labels:\n"
            "    severity: warning\n"
            "  annotations:\n"
            "    summary: 'Longhorn volume {{ $labels.volume }} has been degraded 10m+'\n"
            "    runbook_url: 'https://longhorn.io/docs/...'"
        ),
        has_for_clause=True,
        routing_target="holmesgpt",
        alert_volume_delta="more",
        blast_radius=(
            "Adds one new alert class (LonghornVolumeDegraded). No existing "
            "rule covers this exact condition. HolmesGPT prompts may need a "
            "Longhorn-specific triage pattern added, but the current generic "
            "storage-alert prompt handles it correctly per dry-run."
        ),
        rollback=(
            "Delete the PrometheusRule:\n"
            "  kubectl delete prometheusrule -n storage longhorn-volume-degraded\n"
            "verify: kubectl get prometheusrule -n storage -> rule absent\n"
            "verify Prometheus: no firing/pending instance of LonghornVolumeDegraded"
        ),
        recovery_path_touched=False,
        positive_verification=(
            "1. Apply the rule. 2. Force-degrade a non-production Longhorn "
            "volume via `kubectl scale deployment <consumer> --replicas=0` "
            "and wait for a node restart cycle. 3. Confirm: alert moves "
            "pending → firing at the 10m mark in Prometheus targets UI. "
            "4. Confirm HolmesGPT receives the alert (n8n execution log). "
            "5. Re-scale; confirm alert silences at recovery."
        ),
        action_class="C",
        handoff_target="errand-runner",
        affected_resources=[
            "PrometheusRule: storage/longhorn-volume-degraded",
            "metric: longhorn_volume_state",
            "HolmesGPT generic-storage triage prompt",
        ],
        references=[
            "prom query: longhorn_volume_state{volume=~\".+\"}",
            "longhorn.io/docs alert tuning",
            "24h replay 2026-05-17 → 2026-05-18: typical rebuild <8m, peak 11m",
        ],
    )


def _fake_recovery_path_finding() -> ObservabilityFinding:
    """A change that would silence an alert class → forced user handoff."""
    return ObservabilityFinding(
        summary="User asked to disable the Pushover receiver during a 2-week vacation.",
        flood_mode=(
            "N/A — proposed change silences notifications. By design it would "
            "REDUCE volume to the user's phone. The concern is mute_mode."
        ),
        mute_mode=(
            "Disabling Pushover entirely means every page-worthy alert gets "
            "buried in Zulip (visible-but-not-paging). For a 2-week window, "
            "a real outage (Ceph degraded, HA recorder down, wg-easy offline) "
            "would go unnoticed. The recovery path goes through wg-easy + "
            "OOB SSH pinhole — if those fail, no out-of-band signal."
        ),
        proposed_change=(
            "DEFERRED — disables a receiver entirely. Class D operation; "
            "always-propose. Recommend instead: add a time-bounded silence "
            "(2-week window) on the noisier alert classes (informational "
            "FYIs), keep Pushover active for severity=critical only via a "
            "matcher in AlertmanagerConfig."
        ),
        has_for_clause=False,
        routing_target="pushover",
        alert_volume_delta="fewer",
        blast_radius=(
            "Disables Pushover for the entire cluster. All severity=critical "
            "and severity=warning alerts that route through Pushover would "
            "be muted. Includes Ceph capacity, Longhorn degraded, HA recorder "
            "lag, wg-easy down, BGP peer down, OOB SSH pinhole unreachable."
        ),
        rollback=(
            "Re-enable the Pushover receiver in AlertmanagerConfig:\n"
            "  receivers:\n"
            "    - name: pushover\n"
            "      pushover_configs:\n"
            "        - send_resolved: true\n"
            "          token_file: /etc/...\n"
            "          user_key_file: /etc/..."
        ),
        recovery_path_touched=True,
        positive_verification=(
            "N/A — agent declined the proposed change. The counter-proposal "
            "(severity matcher) would require its own verification: "
            "informational alerts route to Zulip-only; critical alerts "
            "still page; AlertManager logs show matcher hits."
        ),
        action_class="A",
        handoff_target="user",
        affected_resources=["AlertmanagerConfig: observability/main", "receiver: pushover"],
        references=["always-propose: receiver disabling"],
    )


def test_observability_operator_writes_finding_to_vault(temp_vault: Path) -> None:
    state = FleetState(
        task_id="t-obs-001",
        source="zulip",
        content="we should alert on longhorn degraded state, not just faulted",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_safe_finding()

    with patch("agents.nodes.observability_operator._build_llm", return_value=_FakeLLM()):
        result = observability_operator_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "observability-t-obs-001.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-obs-001" in body
    assert "kind: observability-finding" in body
    assert "action_class: C" in body
    assert "handoff_target: errand-runner" in body
    assert "has_for_clause: True" in body
    assert "routing_target: holmesgpt" in body
    assert "alert_volume_delta: more" in body
    # BOTH failure modes must be in the rendered markdown — this is the
    # discipline the schema enforces.
    assert "## Flood mode" in body
    assert "## Mute mode" in body
    assert "Longhorn" in body
    assert "```\nDelete the PrometheusRule" in body
    assert "class=C" in result["output"]
    assert "routing=holmesgpt" in result["output"]
    assert "for_clause=True" in result["output"]


def test_observability_operator_surfaces_recovery_path_touched(temp_vault: Path) -> None:
    """Disabling a receiver / silencing an alert class must surface the warning."""
    state = FleetState(
        task_id="t-obs-002",
        source="text",
        content="disable pushover for 2 weeks while i'm on vacation",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_recovery_path_finding()

    with patch("agents.nodes.observability_operator._build_llm", return_value=_FakeLLM()):
        result = observability_operator_node(state)

    body = (temp_vault / "inbox" / "drafts" / "observability-t-obs-002.md").read_text()
    assert "recovery_path_touched: True" in body
    assert "⚠️ Recovery path touched" in body
    assert "handoff_target: user" in body
    assert "action_class: A" in body
    assert "alert_volume_delta: fewer" in body
    assert "recovery_path=True" in result["output"]


def test_observability_finding_schema_rejects_invalid_routing() -> None:
    """routing_target is Literal[pushover, zulip, holmesgpt, n/a]."""
    with pytest.raises(ValidationError):
        ObservabilityFinding(
            summary="…",
            flood_mode="…",
            mute_mode="…",
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            positive_verification="…",
            action_class="A",
            routing_target="email",  # type: ignore[arg-type]
        )


def test_observability_finding_schema_rejects_invalid_handoff_target() -> None:
    with pytest.raises(ValidationError):
        ObservabilityFinding(
            summary="…",
            flood_mode="…",
            mute_mode="…",
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            positive_verification="…",
            action_class="A",
            handoff_target="coder",  # type: ignore[arg-type]
        )


def test_observability_finding_defaults_safe() -> None:
    """Defaults: handoff=user, has_for_clause=False, routing=n/a, volume=n/a,
    recovery_path_touched=False. These are load-bearing — an LLM that omits
    fields falls through to safest behavior."""
    finding = ObservabilityFinding(
        summary="…",
        flood_mode="…",
        mute_mode="…",
        proposed_change="…",
        blast_radius="…",
        rollback="…",
        positive_verification="…",
        action_class="A",
    )
    assert finding.handoff_target == "user"
    assert finding.has_for_clause is False
    assert finding.routing_target == "n/a"
    assert finding.alert_volume_delta == "n/a"
    assert finding.recovery_path_touched is False
    assert finding.affected_resources == []


def test_observability_finding_requires_both_failure_modes() -> None:
    """flood_mode AND mute_mode are required fields — the discipline this
    role enforces. Omitting either should fail at parse time."""
    with pytest.raises(ValidationError):
        ObservabilityFinding(
            summary="…",
            # flood_mode missing
            mute_mode="…",
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            positive_verification="…",
            action_class="A",
        )
    with pytest.raises(ValidationError):
        ObservabilityFinding(
            summary="…",
            flood_mode="…",
            # mute_mode missing
            proposed_change="…",
            blast_radius="…",
            rollback="…",
            positive_verification="…",
            action_class="A",
        )


def test_observability_operator_persona_loads(temp_vault: Path) -> None:
    invalidate_cache()
    persona = load_persona("observability-operator")
    assert persona
    assert "SOUL — observability-operator" in persona
