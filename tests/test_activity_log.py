"""Activity log writer — drives the reporter's daily-digest aggregation."""

from __future__ import annotations

from pathlib import Path

from agents.tools.activity_log import log_activity


def test_log_activity_creates_file_on_demand(temp_vault: Path) -> None:
    entry = log_activity(
        "triager",
        task_id="t-001",
        action_class="A",
        summary="routed to homelab-engineer (confidence=0.95)",
    )
    log_file = temp_vault / "agents" / "triager" / "memory" / "activity-log.md"
    assert log_file.exists()
    body = log_file.read_text(encoding="utf-8")
    assert "t-001" in body
    assert "class-A" in body
    assert "success" in body  # default outcome
    assert "routed to homelab-engineer" in body
    assert entry.task_id == "t-001"


def test_log_activity_appends(temp_vault: Path) -> None:
    log_activity("reporter", "t-1", action_class="A", summary="first")
    log_activity("reporter", "t-2", action_class="A", summary="second")
    body = (temp_vault / "agents" / "reporter" / "memory" / "activity-log.md").read_text()
    assert "first" in body and "second" in body
    assert body.count("- ") == 2


def test_log_activity_records_outcome(temp_vault: Path) -> None:
    log_activity(
        "errand-runner",
        "t-fail",
        action_class="C",
        summary="ha-mcp.call_service",
        outcome="error",
    )
    body = (temp_vault / "agents" / "errand-runner" / "memory" / "activity-log.md").read_text()
    assert "error" in body
    assert "class-C" in body


def test_log_activity_includes_payload_hash_when_provided(temp_vault: Path) -> None:
    log_activity(
        "errand-runner",
        "t-with-hash",
        action_class="C",
        summary="x",
        payload_hash="abc12345",
    )
    body = (temp_vault / "agents" / "errand-runner" / "memory" / "activity-log.md").read_text()
    assert "abc12345" in body
    assert "payload" in body
