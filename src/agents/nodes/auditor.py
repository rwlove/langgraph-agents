"""auditor — vulnerability researcher.

Cross-references deployed container images vs CVE/GHSA/OSV records.
Produces audit reports; doesn't execute patches.

For v1: persona + LLM. The node does NOT yet directly query kubectl /
GitHub Advisory API / OSV; those integrations land in follow-up PRs.
The persona makes the agent able to REASON about reports a human or a
follow-up workflow feeds it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm import llm
from agents.observability import get_logger
from agents.personas import load_persona
from agents.state import AgentId, FleetState

_AGENT_ID: AgentId = "auditor"
_TEMPERATURE = 0.1  # precise, factual, cited

slog = get_logger("nodes.auditor")


def auditor_node(state: FleetState) -> dict[str, Any]:
    """Produce an audit report or answer an ad-hoc CVE lookup."""
    persona = load_persona(_AGENT_ID)

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}\n\n"
                "Produce a vulnerability finding per your SOUL. Every claim "
                "needs a CVE/GHSA/OSV reference. Score by severity AND cluster "
                "exposure (internal-only vs public-facing). Group by upgrade "
                "path so ADMIN can bundle fixes.\n\n"
                "Note: in v1 you don't yet have direct kubectl / GH Advisory / "
                "OSV access — if image-inventory enumeration or CVE lookup is "
                "required, propose what queries should run (kubectl get hr -A, "
                "POST https://api.osv.dev/v1/query with packages X, Y, Z) and "
                "what you'd do with the results. The actual queries land in a "
                "follow-up that wires the kubectl-mcp + github-mcp + OSV "
                "client into your tool surface."
            )
        ),
    ]

    response = llm(_AGENT_ID, temperature=_TEMPERATURE).invoke(messages)
    output = str(response.content).strip()
    slog.info("audit_report_drafted", out_chars=len(output))

    return {"output": output}
