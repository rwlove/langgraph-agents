"""Persona loader: vault workspace files → composed system prompt.

Each agent has a directory at `vault/agents/workspaces/<id>/` with:
- SOUL.md      — philosophy + voice + red lines (per-agent overlay)
- IDENTITY.md  — name + emoji + creature/vibe
- AGENTS.md    — role + scope + tools + behaviors
- USER.md      — symlink to shared _shared/USER.md (canonical Rob profile)

Plus a shared baseline at `vault/agents/workspaces/_shared/SOUL.md` that every
agent inherits.

The system prompt is the concatenation of: shared SOUL, per-agent SOUL,
IDENTITY, AGENTS, USER. The order is deliberate — philosophy first, then who
you are, then what you do, then who you serve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from agents.settings import get_settings
from agents.state import AgentId


@dataclass(frozen=True)
class Identity:
    """Parsed from IDENTITY.md. Used for Zulip bot display + log labels."""

    name: str
    creature: str | None
    vibe: str | None
    emoji: str


def _read_if_exists(path: Path) -> str | None:
    """Read a file's text, or None if it doesn't exist."""
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _parse_identity(text: str) -> Identity:
    """Parse the IDENTITY.md bullet list into a structured Identity.

    Format expected:
        - **Name:** Triager
        - **Creature:** AI sorter
        - **Vibe:** Sharp and terse — ...
        - **Emoji:** 📥
        - **Avatar:** _(not set)_
    """
    fields: dict[str, str] = {}
    pattern = re.compile(r"-\s+\*\*(?P<key>\w+):\*\*\s+(?P<val>.+)")
    for line in text.splitlines():
        m = pattern.search(line.strip())
        if m:
            fields[m["key"].lower()] = m["val"].strip()
    return Identity(
        name=fields.get("name", "Agent"),
        creature=fields.get("creature"),
        vibe=fields.get("vibe"),
        emoji=fields.get("emoji", "🤖"),
    )


@cache
def load_persona(agent_id: AgentId) -> str:
    """Compose the full system prompt for one agent.

    Cached because vault content is stable within a runtime; the file-watcher
    in tools/skills.py invalidates the cache when files change.
    """
    settings = get_settings()
    agent_dir = settings.workspaces_dir / agent_id
    shared_dir = settings.shared_workspace_dir

    if not agent_dir.is_dir():
        raise FileNotFoundError(
            f"No workspace dir for agent '{agent_id}' at {agent_dir}. "
            f"Sync vault to PVC and ensure the workspaces/<id>/ directory exists."
        )

    sections: list[tuple[str, str]] = []

    shared_soul = _read_if_exists(shared_dir / "SOUL.md")
    if shared_soul:
        sections.append(("SHARED SOUL", shared_soul))

    for filename in ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md"):
        text = _read_if_exists(agent_dir / filename)
        if text:
            sections.append((filename.replace(".md", ""), text))

    if not sections:
        raise ValueError(f"Agent '{agent_id}' workspace dir is empty: {agent_dir}")

    return "\n\n---\n\n".join(body for _, body in sections)


@cache
def load_identity(agent_id: AgentId) -> Identity:
    """Load the parsed IDENTITY for an agent. Used for Zulip / activity labels."""
    settings = get_settings()
    identity_text = _read_if_exists(settings.workspaces_dir / agent_id / "IDENTITY.md")
    if not identity_text:
        return Identity(name=agent_id, creature=None, vibe=None, emoji="🤖")
    return _parse_identity(identity_text)


def invalidate_cache() -> None:
    """Drop the cached personas + identities. Call when vault content changes."""
    load_persona.cache_clear()
    load_identity.cache_clear()
