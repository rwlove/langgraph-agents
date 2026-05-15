"""note-maker node — LLM is mocked; we verify the draft is rendered + written."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents.nodes.note_maker import NoteDraft, note_maker_node
from agents.state import FleetState


def _fake_draft() -> NoteDraft:
    body = (
        "The porch light hasn't fired at sunset for the last 3 days. "
        "Suspect sun trigger drift or sensor fault."
    )
    return NoteDraft(
        title="Porch light not turning on at sunset",
        domain="smart-home",
        body=body,
        tags=["porch-light", "sun-trigger"],
        proposed_location="~/vaults/personal/smart-home/porch-light-sunset.md",
        related=["[[home-assistant-config]]"],
        new_vs_append="new",
    )


def test_note_maker_writes_draft_and_returns_path(temp_vault: Path) -> None:
    """The node should produce a draft file under inbox/drafts/note-<task_id>.md."""
    state = FleetState(
        task_id="t-100",
        source="voice",
        content="hey uh the porch light isn't turning on at sunset anymore",
    )

    class _FakeLLM:
        def invoke(self, _messages):
            return _fake_draft()

    with patch("agents.nodes.note_maker._build_llm", return_value=_FakeLLM()):
        result = note_maker_node(state)

    expected_path = temp_vault / "inbox" / "drafts" / "note-t-100.md"
    assert expected_path.exists()
    body = expected_path.read_text(encoding="utf-8")
    assert "task_id: t-100" in body
    assert "domain: smart-home" in body
    assert "Porch light not turning on at sunset" in body
    assert "#porch-light" in body
    assert "[[home-assistant-config]]" in body
    assert "note drafted" in result["output"]
    assert "smart-home" in result["output"]


def test_note_maker_handles_empty_tags(temp_vault: Path) -> None:
    """No tags should not produce an empty '## Tags' section."""
    draft = _fake_draft().model_copy(update={"tags": []})

    class _FakeLLM:
        def invoke(self, _messages):
            return draft

    state = FleetState(task_id="t-101", source="text", content="…")

    with patch("agents.nodes.note_maker._build_llm", return_value=_FakeLLM()):
        note_maker_node(state)

    body = (temp_vault / "inbox" / "drafts" / "note-t-101.md").read_text()
    assert "## Tags" not in body


def test_note_draft_schema_rejects_invalid_domain() -> None:
    """The structured-output schema mirrors the typed routing constraints."""
    with pytest.raises(ValidationError):
        NoteDraft(
            title="x",
            domain="not-a-domain",  # type: ignore[arg-type]
            body="…",
            proposed_location="…",
        )
