"""Pytest fixtures.

Points the runtime at a temp vault populated with minimal workspaces, so
tests don't depend on the real laptop vault being present.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agents.settings import Settings, get_settings


def _populate_temp_vault(root: Path) -> None:
    """Lay down a minimal vault structure with 14 stub workspaces + shared."""
    workspaces = root / "agents" / "workspaces"
    shared = workspaces / "_shared"
    shared.mkdir(parents=True)
    (shared / "SOUL.md").write_text("# Shared SOUL\n\nBaseline.\n")
    (shared / "USER.md").write_text("# USER\n\nRob.\n")

    agent_ids = [
        "triager",
        "historian",
        "note-maker",
        "researcher",
        "coder",
        "errand-runner",
        "supervisor",
        "reviewer",
        "homelab-engineer",
        "network-operator",
        "storage-operator",
        "smart-home-operator",
        "ml-operator",
        "observability-operator",
        "health-tracker",
        "property-coordinator",
        "doc-writer",
    ]
    for agent in agent_ids:
        d = workspaces / agent
        d.mkdir()
        (d / "SOUL.md").write_text(f"# SOUL — {agent}\n\nVoice for {agent}.\n")
        (d / "IDENTITY.md").write_text(
            f"# IDENTITY\n\n"
            f"- **Name:** {agent.title()}\n"
            f"- **Creature:** AI {agent}\n"
            f"- **Vibe:** test\n"
            f"- **Emoji:** 🤖\n"
        )
        (d / "AGENTS.md").write_text(f"# AGENTS — {agent}\n\nRole: {agent}.\n")
        # USER.md as a real file (not symlink) — simpler for tests
        (d / "USER.md").write_text("# USER\n\nRob.\n")


@pytest.fixture
def temp_vault(tmp_path: Path) -> Iterator[Path]:
    """A populated temp vault. Sets VAULT_ROOT + AGENT_WORKSPACES_DIR env.

    VAULT_ROOT still drives skills_dir + obsidian write paths (which remain
    vault-side). AGENT_WORKSPACES_DIR redirects the persona loader at the
    temp fixture's workspaces subtree (post-stage-3, the loader otherwise
    resolves to the repo-baked agents/workspaces/).
    """
    vault = tmp_path / "claude-vault"
    vault.mkdir()
    _populate_temp_vault(vault)

    prev_vault = os.environ.get("VAULT_ROOT")
    prev_workspaces = os.environ.get("AGENT_WORKSPACES_DIR")
    os.environ["VAULT_ROOT"] = str(vault)
    os.environ["AGENT_WORKSPACES_DIR"] = str(vault / "agents" / "workspaces")
    get_settings.cache_clear()

    yield vault

    for key, prev in (("VAULT_ROOT", prev_vault), ("AGENT_WORKSPACES_DIR", prev_workspaces)):
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev
    get_settings.cache_clear()


@pytest.fixture
def settings(temp_vault: Path) -> Settings:
    return get_settings()
