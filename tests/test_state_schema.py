"""The state schema is load-bearing: typed routing targets fail at parse time
rather than producing invalid routes at runtime. These tests anchor that
guarantee."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.state import (
    ALL_AGENT_IDS,
    ApprovalRequest,
    FleetState,
    RejectionSignal,
    RetryPolicy,
    TriageDecision,
)


def test_all_agent_ids_unique() -> None:
    assert len(set(ALL_AGENT_IDS)) == len(ALL_AGENT_IDS)


def test_all_agent_ids_count() -> None:
    # 13 fleet agents + doc-writer (TODO 9, added 2026-05-15)
    # + network-operator (ported from claude-personal subagent, 2026-05-17)
    # + storage-operator (net-new, 2026-05-18)
    # + smart-home-operator (promoted from smart-home-engineer, 2026-05-18)
    # + ml-operator (promoted from ml-tuner, 2026-05-18)
    # + observability-operator (net-new, 2026-05-18)
    # + reporter (final-hop user-facing messenger, 2026-05-23)
    # + artist (ComfyUI image generation via artokun/comfyui-mcp, 2026-05-23)
    # + security (surveillance + physical-security analyst, 2026-05-23)
    # + auditor (vulnerability researcher, 2026-05-23)
    assert len(ALL_AGENT_IDS) == 21


def test_triage_decision_accepts_valid_target() -> None:
    decision = TriageDecision(
        summary="Pod is restarting",
        domain="homelab",
        intent="bug",
        target_agent="homelab-engineer",
        confidence=0.9,
        reasoning="Clearly a homelab issue.",
    )
    assert decision.target_agent == "homelab-engineer"


def test_triage_decision_rejects_invalid_target() -> None:
    with pytest.raises(ValidationError):
        TriageDecision(
            summary="…",
            domain="homelab",
            intent="bug",
            target_agent="nonexistent-agent",  # type: ignore[arg-type]
            confidence=0.5,
            reasoning="…",
        )


def test_triage_decision_rejects_invalid_domain() -> None:
    with pytest.raises(ValidationError):
        TriageDecision(
            summary="…",
            domain="banking",  # type: ignore[arg-type]
            intent="question",
            target_agent="triager",
            confidence=0.5,
            reasoning="…",
        )


def test_triage_decision_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        TriageDecision(
            summary="…",
            domain="homelab",
            intent="question",
            target_agent="homelab-engineer",
            confidence=1.5,  # > 1
            reasoning="…",
        )


def test_rejection_signal_typed_target() -> None:
    sig = RejectionSignal(
        rejected_by="coder",
        reason="this is infra, not general code",
        suggested_target="homelab-engineer",
        context_to_preserve="task summary",
    )
    assert sig.suggested_target == "homelab-engineer"


def test_approval_request_action_class_typed() -> None:
    req = ApprovalRequest(
        action_class="C",
        target="deployment/frigate",
        payload_summary="rollout restart",
        undo_path="kubectl rollout undo deployment/frigate",
        proposed_by="errand-runner",
    )
    assert req.action_class == "C"

    with pytest.raises(ValidationError):
        ApprovalRequest(
            action_class="X",  # type: ignore[arg-type]
            target="…",
            payload_summary="…",
            proposed_by="triager",
        )


def test_fleet_state_minimal_constructible() -> None:
    state = FleetState(
        task_id="t-001",
        source="test",
        content="hello",
    )
    assert state.task_id == "t-001"
    assert state.cascade_count == 0
    assert state.triage is None


# --- Task envelope (HOMELAB-SPEC Layer 5; additive) -------------------------


def test_fleet_state_envelope_defaults() -> None:
    """Old callers (no envelope fields) get the documented defaults."""
    state = FleetState(task_id="t-001", source="test", content="hi")
    assert state.priority == "normal"
    assert state.data_tier == "internal"
    assert state.trace_id is None
    assert state.origin is None
    assert state.requester is None
    assert state.intent_envelope is None
    assert state.destructive is None
    assert state.idempotency_key is None
    assert state.ttl_seconds is None
    assert state.retry_policy is None


def test_fleet_state_envelope_full() -> None:
    """New callers can populate the full envelope."""
    state = FleetState(
        task_id="t-002",
        source="openwebui",
        content="hi",
        trace_id="01HXYZABC",
        origin="open-webui",
        requester="rob",
        intent_envelope="action",
        priority="urgent",
        destructive=True,
        idempotency_key="dedup-key",
        ttl_seconds=600,
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=5),
        data_tier="restricted",
    )
    assert state.priority == "urgent"
    assert state.data_tier == "restricted"
    assert state.trace_id == "01HXYZABC"
    assert state.requester == "rob"
    assert state.retry_policy is not None
    assert state.retry_policy.max_attempts == 3


def test_fleet_state_envelope_rejects_invalid_enum() -> None:
    """Typed enums reject unknown values."""
    with pytest.raises(ValidationError):
        FleetState(
            task_id="t-003",
            source="test",
            content="hi",
            priority="critical",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        FleetState(
            task_id="t-004",
            source="test",
            content="hi",
            data_tier="secret",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        FleetState(
            task_id="t-005",
            source="test",
            content="hi",
            requester="anonymous",  # type: ignore[arg-type]
        )


def test_retry_policy_bounds() -> None:
    """max_attempts >= 1 and backoff_seconds >= 0."""
    RetryPolicy(max_attempts=1, backoff_seconds=0)
    RetryPolicy(max_attempts=5, backoff_seconds=10)
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValidationError):
        RetryPolicy(backoff_seconds=-1)
