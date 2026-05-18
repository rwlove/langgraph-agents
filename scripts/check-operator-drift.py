#!/usr/bin/env python3
"""Check Claude Code subagent .md ↔ langgraph vault for operator drift.

The same operator concept (prime directive + execution gate + scope) is
authored in two forms:

- **Claude Code subagent**: `~/.claude-personal/agents/<id>.md` (one file
  with frontmatter; used by Claude Code on the laptop).
- **Langgraph vault**: `~/vaults/claude/agents/workspaces/<id>/{SOUL,
  IDENTITY,AGENTS,USER}.md` (four files; loaded by personas.py at
  pod startup).

Both are hand-authored. This script doesn't enforce textual equality —
it asserts **structural parity**: every operator-class agent should
exist in both forms with the same slug, and key safety phrases ("prime
directive", the operator's specific directive) should appear in both.

Run from the langgraph-agents repo root:

    python scripts/check-operator-drift.py

Exit 0 if parity holds; exit 1 with a report otherwise.

Note: this is a *laptop-local* drift check. The .md files don't live
in any git repo, so this can't be a CI gate — run it before authoring
commits that touch operator personas.
"""

from __future__ import annotations

import sys
from pathlib import Path

CLAUDE_AGENTS = Path.home() / ".claude-personal" / "agents"
VAULT_WORKSPACES = Path.home() / "vaults" / "claude" / "agents" / "workspaces"

# Operator-class agents we expect in both forms. Update when promoting an
# engineer/tuner to operator-class.
OPERATOR_SLUGS = (
    "network-operator",
    "storage-operator",
    "smart-home-operator",
    "ml-operator",
)


def md_has_phrase(md_path: Path, phrase: str) -> bool:
    """Case-insensitive substring search in a markdown file."""
    if not md_path.is_file():
        return False
    return phrase.lower() in md_path.read_text(encoding="utf-8").lower()


def check_operator(slug: str) -> list[str]:
    """Return a list of drift findings for one operator. Empty = parity holds."""
    findings: list[str] = []

    md = CLAUDE_AGENTS / f"{slug}.md"
    vault_dir = VAULT_WORKSPACES / slug
    soul = vault_dir / "SOUL.md"
    identity = vault_dir / "IDENTITY.md"
    agents = vault_dir / "AGENTS.md"

    if not md.is_file():
        findings.append(f"  MISSING .md: {md}")
    if not vault_dir.is_dir():
        findings.append(f"  MISSING vault dir: {vault_dir}")
        return findings  # nothing more to check
    for name, path in (("SOUL.md", soul), ("IDENTITY.md", identity), ("AGENTS.md", agents)):
        if not path.is_file():
            findings.append(f"  MISSING vault file: {path}")

    if md.is_file() and soul.is_file():
        if not md_has_phrase(md, "prime directive"):
            findings.append(f"  .md missing 'prime directive' section: {md}")
        if not md_has_phrase(soul, "prime directive"):
            findings.append(f"  vault SOUL missing 'prime directive': {soul}")

    if md.is_file() and identity.is_file():
        if not md_has_phrase(md, "execution gate"):
            findings.append(f"  .md missing 'execution gate' section: {md}")
        if not md_has_phrase(identity, "execution gate"):
            findings.append(f"  vault IDENTITY missing 'execution gate': {identity}")

    return findings


def main() -> int:
    print(f"Checking {len(OPERATOR_SLUGS)} operators for .md ↔ vault parity…\n")
    any_drift = False
    for slug in OPERATOR_SLUGS:
        findings = check_operator(slug)
        if findings:
            any_drift = True
            print(f"✗ {slug}")
            for f in findings:
                print(f)
        else:
            print(f"✓ {slug}")
    if any_drift:
        print("\nDrift detected. Reconcile .md ↔ vault before committing.")
        return 1
    print("\nParity holds across all operator-class agents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
