"""auditor — vulnerability researcher.

Enumerates deployed container images via kubectl-mcp tool calls,
correlates against CVE/GHSA/OSV records (when those clients ship in
a follow-up — v1 reasons over kubectl data only), and produces
audit reports. Read-only; never executes patches.

## ReAct loop (since 2026-05-25, PR-S)

Auditor was the first agent rewired off the original
`with_structured_output()` over `state.content` pattern (memory
`reference_agent_fleet_tool_binding_gap`). It now uses LangGraph's
`create_react_agent` so the LLM can iteratively call kubectl-mcp
methods (get, describe, logs, events, top) to gather the data it
needs instead of waiting for the workflow to pre-fetch everything.

The tool surface is auto-built from `tools/mcp.py:ALLOWLISTS["auditor"]`
via `tools/mcp_langchain.py:build_mcp_tools_for_agent()`. Adding a
capability is still a single-source-of-truth change in `tools/mcp.py`.

If the LLM iterates past `recursion_limit` without a final answer
(qwen2.5:32b sometimes loops on tool calls), we capture whatever
text was produced and return it; the cron's downstream reporter
node renders the result for the user. The recursion limit defaults
to 25 — generous for an audit walk; tightened if costs become a
concern.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from agents.llm import llm
from agents.observability import get_logger
from agents.personas import load_persona
from agents.state import AgentId, FleetState
from agents.tools.mcp_langchain import build_mcp_tools_for_agent

_AGENT_ID: AgentId = "auditor"
_TEMPERATURE = 0.1  # precise, factual, cited

# How many think-act-observe rounds the LLM can take before we cut it
# off. qwen2.5:32b under tool-use can wander; 25 is enough for a real
# image-enumeration audit (~5 kubectl calls + reasoning between each)
# while still bounding latency.
_RECURSION_LIMIT = 25

slog = get_logger("nodes.auditor")


def auditor_node(state: FleetState) -> dict[str, Any]:
    """Produce an audit report by iteratively calling kubectl-mcp tools."""
    persona = load_persona(_AGENT_ID)
    tools = build_mcp_tools_for_agent(_AGENT_ID)

    system_prompt = (
        f"{persona}\n\n"
        "You have read-only kubectl access to the cluster via the tools "
        "listed below. Use them to gather the evidence you need — do NOT "
        "guess about cluster state. Cite specific resources you queried.\n\n"
        "Vulnerability database access (OSV/GHSA) is not yet wired into "
        "your tool surface. When you'd need a CVE lookup, name the "
        "query the operator should run (e.g. "
        "`curl -X POST https://api.osv.dev/v1/query -d '{\"package\": "
        "{\"name\": \"X\"}}'`) and what you'd do with the result. The "
        "actual queries land in a follow-up that wires an OSV client.\n\n"
        "When you have enough evidence to produce a report, write it "
        "as the final assistant message — ranked findings (highest-risk "
        "first), each with evidence + one-sentence 'why it matters' + a "
        "single recommended next step. No more tool calls after the "
        "report. Keep total output under 2000 chars."
    )

    agent = create_react_agent(
        model=llm(_AGENT_ID, temperature=_TEMPERATURE),
        tools=tools,
        prompt=SystemMessage(content=system_prompt),
    )

    config: RunnableConfig = {"recursion_limit": _RECURSION_LIMIT}
    inputs = {
        "messages": [
            HumanMessage(content=f"REQUEST:\n\n{state.content}"),
        ],
    }

    try:
        final_state = agent.invoke(inputs, config=config)
    except GraphRecursionError:
        # Hit the recursion limit before producing a final answer.
        # Capture whatever text was emitted so the operator at least
        # sees the agent's intermediate reasoning. Common qwen2.5:32b
        # failure mode under tool-use.
        slog.warning(
            "audit_recursion_limit_hit",
            agent=_AGENT_ID,
            recursion_limit=_RECURSION_LIMIT,
        )
        return {
            "output": (
                f"⚠️ auditor hit recursion limit ({_RECURSION_LIMIT}) "
                "before producing a final report. Re-fire with a more "
                "focused REQUEST scope to reduce tool-loop depth."
            ),
        }

    # The final answer is the last AIMessage's content (no tool_calls).
    # If the model ended with a tool_call instead of prose, that means
    # it didn't synthesize — capture the warning.
    messages = final_state.get("messages", [])
    if not messages:
        return {"output": "⚠️ auditor produced no messages."}

    last = messages[-1]
    last_text = ""
    if hasattr(last, "content"):
        last_text = str(last.content).strip()

    # Count tool calls + iterations for observability.
    tool_call_count = sum(
        1 for m in messages if hasattr(m, "tool_calls") and m.tool_calls
    )
    slog.info(
        "audit_report_drafted",
        out_chars=len(last_text),
        message_count=len(messages),
        tool_call_count=tool_call_count,
    )

    if not last_text:
        return {
            "output": (
                "⚠️ auditor ended with a tool_call instead of prose. "
                "The LLM didn't finalize the report. Re-fire."
            ),
        }

    return {"output": last_text}
