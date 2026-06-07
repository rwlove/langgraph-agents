"""Load golden task sets and rubrics from disk."""

from __future__ import annotations

from pathlib import Path

import yaml

from evals.schema import GoldenSet

_HERE = Path(__file__).resolve().parent
GOLDEN_DIR = _HERE / "golden"
RUBRIC_DIR = _HERE / "rubrics"


def load_golden_set(path: Path) -> GoldenSet:
    """Parse + validate a golden-set YAML file into a `GoldenSet`."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return GoldenSet.model_validate(data)


def load_golden_for(agent_id: str) -> GoldenSet:
    """Load ``golden/<agent_id>.yaml``."""
    return load_golden_set(GOLDEN_DIR / f"{agent_id}.yaml")


def available_agents() -> list[str]:
    """Agent ids that have a golden set on disk, sorted."""
    return sorted(p.stem for p in GOLDEN_DIR.glob("*.yaml"))


def load_rubric(agent_id: str) -> str:
    """Per-agent rubric if present, else the default rubric."""
    specific = RUBRIC_DIR / f"{agent_id}.md"
    if specific.exists():
        return specific.read_text(encoding="utf-8")
    return (RUBRIC_DIR / "default.md").read_text(encoding="utf-8")
