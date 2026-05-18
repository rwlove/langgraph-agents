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
    assert len(ALL_AGENT_IDS) == 16


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
