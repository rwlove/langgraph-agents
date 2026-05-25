"""Vault read/write helpers.

The vault is mounted RW for the langgraph-agents pod. These helpers are the
*only* code path that writes vault files outside per-agent memory. Anything
landing in `inbox/drafts/`, `reports/`, or agent memory goes through here so
the audit story is clean (one well-known entry point per write category).

Read-only-suggest agents (reviewer) and personal-vault-blocked agents
(everyone except health-tracker for medical) get a constructor variant that
restricts the writable root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from agents.settings import get_settings


def _slugify(text: str, max_len: int = 40) -> str:
    """File-name safe slug. Keeps letters/digits/dashes, lowercased."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "untitled"


@dataclass(frozen=True)
class WriteResult:
    """Returned from every write helper. Use the path for downstream state."""

    path: Path
    bytes_written: int

    def obsidian_uri(self, vault_name: str = "claude") -> str:
        """Return an obsidian:// deep-link for this file.

        Strips the vault root from the absolute path so the URI contains only
        the vault-relative portion (e.g. ``inbox/drafts/network-t-net-001.md``).
        Falls back to the filename alone if the path isn't under the vault root.
        """
        settings = get_settings()
        try:
            rel = self.path.relative_to(settings.vault_root)
        except ValueError:
            rel = Path(self.path.name)
        return f"obsidian://open?vault={quote(vault_name)}&file={quote(str(rel))}"


@dataclass(frozen=True)
class VaultGrepHit:
    """A match from grep_vault_memory."""

    path: Path
    line_number: int
    line: str

    def excerpt(self, max_len: int = 120) -> str:
        return self.line.strip()[:max_len]


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_atomic(path: Path, content: str) -> int:
    """Write via tmp + rename. LiveSync's .tmp. ignore-regex (set 2026-05-14)
    keeps the tmp files out of CouchDB replication."""
    _ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp." + datetime.now(UTC).strftime("%Y%m%d%H%M%S"))
    encoded = content.encode("utf-8")
    tmp.write_bytes(encoded)
    tmp.replace(path)
    return len(encoded)


def write_draft(task_id: str, body: str, *, kind: str = "note") -> WriteResult:
    """Write a draft to `vault/inbox/drafts/<kind>-<task_id>.md`.

    Used by note-maker, health-tracker, and anyone else producing a
    user-reviewable draft. Drafts are NEVER written to the personal vault
    directly; the user moves them after review.
    """
    settings = get_settings()
    drafts_dir = settings.vault_root / "inbox" / "drafts"
    path = drafts_dir / f"{kind}-{task_id}.md"
    n = _write_atomic(path, body)
    return WriteResult(path=path, bytes_written=n)


def write_finding(task_id: str, topic: str, body: str) -> WriteResult:
    """Write a research finding to `vault/reports/research/<task_id>-<slug>.md`.

    Used by the researcher node. The slug helps humans browse the directory
    without consulting the task_id.
    """
    settings = get_settings()
    research_dir = settings.vault_root / "reports" / "research"
    slug = _slugify(topic)
    path = research_dir / f"{task_id}-{slug}.md"
    n = _write_atomic(path, body)
    return WriteResult(path=path, bytes_written=n)


def grep_vault_memory(
    pattern: str,
    *,
    max_hits: int = 50,
    case_insensitive: bool = True,
) -> list[VaultGrepHit]:
    """Naive grep over `vault/projects/*/memory/*.md` and `vault/user/memory/*.md`.

    The researcher's first stop. Returns matched lines with paths; the caller
    decides which hits warrant a full read.
    """
    settings = get_settings()
    flags = re.IGNORECASE if case_insensitive else 0
    rx = re.compile(pattern, flags)

    roots = [
        settings.vault_root / "projects",
        settings.vault_root / "user" / "memory",
    ]

    hits: list[VaultGrepHit] = []
    for root in roots:
        if not root.exists():
            continue
        for md in root.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(VaultGrepHit(path=md, line_number=lineno, line=line))
                    if len(hits) >= max_hits:
                        return hits
    return hits
