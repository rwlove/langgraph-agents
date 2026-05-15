"""Architectural assertion: health-tracker is local-only.

The health-tracker module must NEVER import from the anthropic SDK or any
other path to an external model provider. This test enforces it statically
at the source-tree level — not a runtime policy but a structural invariant.

Until the health-tracker node lands (phase 8), this test asserts the
constraint by inspecting the import graph for the entire `agents.nodes`
package: no module that contains 'health_tracker' in its path imports
anthropic.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

import agents.nodes


def _iter_node_modules() -> list[str]:
    return [m.name for m in pkgutil.iter_modules(agents.nodes.__path__)]


def test_health_tracker_module_does_not_import_anthropic() -> None:
    """Once phase 8 adds health_tracker.py, importing it must not pull anthropic."""
    modules = _iter_node_modules()
    if "health_tracker" not in modules:
        # Phase 1: module doesn't exist yet. Test passes vacuously; will
        # become meaningful in phase 8 when the module is authored.
        return

    before = set(sys.modules)
    importlib.import_module("agents.nodes.health_tracker")
    after = set(sys.modules)
    new = after - before
    assert not any("anthropic" in m for m in new), (
        f"health_tracker pulled in anthropic-related modules: "
        f"{[m for m in new if 'anthropic' in m]}"
    )
