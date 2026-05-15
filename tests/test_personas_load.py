"""Persona loader smoke tests against a temp vault."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.personas import invalidate_cache, load_identity, load_persona
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS


@pytest.fixture(autouse=True)
def _reset_cache(temp_vault: Path) -> None:
    # temp_vault changes VAULT_ROOT; clear the per-agent cache so each test
    # reads the fresh path.
    invalidate_cache()


def test_load_persona_for_every_agent(temp_vault: Path) -> None:
    """All 13 agents must have a loadable persona."""
    for agent_id in ALL_AGENT_IDS:
        prompt = load_persona(agent_id)
        assert prompt, f"empty persona for {agent_id}"
        assert "SHARED SOUL" in prompt or "Shared SOUL" in prompt
        # Per-agent SOUL section should be present
        assert f"SOUL — {agent_id}" in prompt


def test_load_persona_missing_dir_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent without a workspace dir is a deployment bug — fail loudly."""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    invalidate_cache()

    with pytest.raises(FileNotFoundError):
        load_persona("triager")


def test_load_identity_parses_name_and_emoji(temp_vault: Path) -> None:
    identity = load_identity("triager")
    assert identity.name == "Triager"
    assert identity.emoji == "🤖"
    assert identity.creature == "AI triager"


def test_load_identity_for_every_agent(temp_vault: Path) -> None:
    for agent_id in ALL_AGENT_IDS:
        identity = load_identity(agent_id)
        assert identity.name, f"missing name for {agent_id}"
        assert identity.emoji, f"missing emoji for {agent_id}"
