"""Vault write helpers + grep against the temp vault fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.tools.obsidian import (
    grep_vault_memory,
    write_draft,
    write_finding,
)


def test_write_draft_creates_inbox_drafts_dir(temp_vault: Path) -> None:
    result = write_draft("t-001", "# hello\n\nbody\n", kind="note")
    assert result.path == temp_vault / "inbox" / "drafts" / "note-t-001.md"
    assert result.path.exists()
    assert result.path.read_text(encoding="utf-8") == "# hello\n\nbody\n"
    assert result.bytes_written == len(b"# hello\n\nbody\n")


def test_write_draft_kind_prefix(temp_vault: Path) -> None:
    result = write_draft("t-002", "body", kind="medical")
    assert result.path.name == "medical-t-002.md"


def test_write_finding_slugifies_topic(temp_vault: Path) -> None:
    result = write_finding(
        "t-003",
        topic="What's the deal with the QMD index?",
        body="# findings\n",
    )
    assert result.path.parent == temp_vault / "reports" / "research"
    # Slug should be lowercased, dashes, no punctuation
    assert "what-s-the-deal" in result.path.name or "what" in result.path.name
    assert result.path.name.startswith("t-003-")


def test_grep_vault_memory_returns_matches(temp_vault: Path) -> None:
    # Populate a fake project memory dir
    proj = temp_vault / "projects" / "demo" / "memory"
    proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("frigate detection accuracy\nlongorhn snapshot\n")
    (proj / "project_todo_x.md").write_text("Frigate retraining backlog item\n")

    hits = grep_vault_memory("frigate")
    assert len(hits) == 2
    assert {h.path.name for h in hits} == {"MEMORY.md", "project_todo_x.md"}


def test_grep_vault_memory_respects_max_hits(temp_vault: Path) -> None:
    proj = temp_vault / "projects" / "many" / "memory"
    proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("\n".join(["match this"] * 20))

    hits = grep_vault_memory("match", max_hits=5)
    assert len(hits) == 5


def test_grep_vault_memory_case_sensitivity_toggle(temp_vault: Path) -> None:
    proj = temp_vault / "projects" / "case" / "memory"
    proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("UpperCase token here\n")

    assert grep_vault_memory("uppercase", case_insensitive=True)
    assert not grep_vault_memory("uppercase", case_insensitive=False)


def test_grep_vault_memory_no_matches_returns_empty(temp_vault: Path) -> None:
    proj = temp_vault / "projects" / "empty" / "memory"
    proj.mkdir(parents=True)
    (proj / "MEMORY.md").write_text("nothing relevant\n")

    assert grep_vault_memory("nonexistent-token") == []


@pytest.mark.parametrize(
    "topic,expected_substring",
    [
        ("Plain text", "plain-text"),
        ("UPPERCASE", "uppercase"),
        ("punctuation!! all over.?", "punctuation-all-over"),
        ("a" * 100, "a" * 40),  # max_len cap at 40
    ],
)
def test_slugify_via_write_finding(temp_vault: Path, topic: str, expected_substring: str) -> None:
    result = write_finding("t-x", topic=topic, body="x")
    assert expected_substring in result.path.name
