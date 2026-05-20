"""Operator persona drift check.

Wraps the standalone `scripts/check-operator-drift.py` as a pytest case
so `uv run pytest` catches drift between the Claude Code subagent .md
files and the langgraph vault workspaces locally during development.

CI environments don't have access to the user's `~/.claude-personal/`
or `~/vaults/` paths — the test skips when either is missing, so it
stays a useful pre-commit guardrail without breaking CI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_DRIFT_SCRIPT = _SCRIPTS_DIR / "check-operator-drift.py"


def _load_drift_module() -> object:
    """Load the hyphenated script as a Python module under the name
    ``check_operator_drift`` so we can call ``check_operator()`` directly
    instead of shelling out and parsing stdout."""
    spec = importlib.util.spec_from_file_location("check_operator_drift", _DRIFT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_operator_drift"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def drift_module() -> object:
    if not _DRIFT_SCRIPT.is_file():
        pytest.skip(f"drift-check script missing: {_DRIFT_SCRIPT}")
    return _load_drift_module()


def test_operator_drift(drift_module: object) -> None:
    """Assert .md ↔ vault parity for every operator-class agent.

    Skips when the local paths the script reads aren't present — that's
    expected in CI; the test stays useful for laptop development.
    """
    claude_agents = drift_module.CLAUDE_AGENTS  # type: ignore[attr-defined]
    vault_workspaces = drift_module.VAULT_WORKSPACES  # type: ignore[attr-defined]
    if not claude_agents.is_dir() or not vault_workspaces.is_dir():
        pytest.skip(f"laptop-only drift check: missing {claude_agents} or {vault_workspaces}")

    drifts: dict[str, list[str]] = {}
    for slug in drift_module.OPERATOR_SLUGS:  # type: ignore[attr-defined]
        findings = drift_module.check_operator(slug)  # type: ignore[attr-defined]
        if findings:
            drifts[slug] = findings

    if drifts:
        formatted = "\n".join(
            f"  {slug}:\n" + "\n".join(f"    {f}" for f in findings)
            for slug, findings in drifts.items()
        )
        pytest.fail(f"Operator drift detected:\n{formatted}")
