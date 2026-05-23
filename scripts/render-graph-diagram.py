#!/usr/bin/env python3
"""Render the compiled fleet graph as a Mermaid diagram.

LangGraph's `CompiledGraph.get_graph().draw_mermaid()` generates a Mermaid
string from the actual compiled state machine — single source of truth, no
hand-drawn approximation. Embed the result in README.md + PIPELINE.md so
the docs stay current with the wiring.

Usage:
    scripts/render-graph-diagram.py              # print to stdout
    scripts/render-graph-diagram.py --write      # update fleet-graph.mmd

Run after any change to `src/agents/graphs/fleet.py` that touches the
node set or the conditional edges. The CI lint could enforce the
`fleet-graph.mmd` is up-to-date if drift becomes a recurring issue.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `agents` importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.graphs.fleet import build_fleet_graph


def render() -> str:
    """Compile the fleet graph and return its Mermaid representation."""
    graph = build_fleet_graph(checkpointer=None, store=None)
    return graph.get_graph().draw_mermaid()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write to docs/fleet-graph.mmd (next to README.md).",
    )
    args = parser.parse_args()

    mermaid = render()

    if args.write:
        out = Path(__file__).resolve().parent.parent / "docs" / "fleet-graph.mmd"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(mermaid)
        print(f"wrote {out} ({len(mermaid)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(mermaid)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
