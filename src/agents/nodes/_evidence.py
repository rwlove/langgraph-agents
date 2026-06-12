"""Shared evidence-gathering pre-pass for operator nodes."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from agents.llm import llm
from agents.observability import get_logger
from agents.settings import get_settings
from agents.state import AgentId
from agents.tools.mcp_langchain import build_mcp_tools_for_agent

# The evidence sub-agent is a ReAct loop. Each tool call costs ~2 super-steps
# (agent -> tool -> agent), so a budget of N tool calls needs ~2N+1 steps. Keep
# the recursion limit comfortably above the advertised tool budget, or the loop
# trips GraphRecursionError before it can finish its own instructions and the
# whole pre-pass yields nothing. See langgraph-agents#130.
_MAX_TOOL_CALLS = 8
_RECURSION_LIMIT = 2 * _MAX_TOOL_CALLS + 4  # = 20; headroom over the ~17-step budget
slog = get_logger("nodes.evidence")


def _extract_evidence(messages: list[BaseMessage]) -> str:
    """Pull a text evidence block out of a (possibly truncated) message list.

    Happy path: the sub-agent emitted a final summary — return it verbatim.
    Salvage path (recursion limit hit, or the last turn was a pending tool
    call): stitch the raw tool observations gathered so far, so a stalled loop
    still contributes evidence instead of nothing (langgraph-agents#130).
    """
    if not messages:
        return ""

    last = messages[-1]
    if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
        summary = str(last.content).strip()
        if summary:
            return summary

    salvaged = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            observation = str(getattr(msg, "content", "")).strip()
            if observation:
                name = getattr(msg, "name", None) or "tool"
                salvaged.append(f"- {name}: {observation}")
    return "\n".join(salvaged)


async def gather_evidence(agent_id: AgentId, request: str) -> str:
    """Run a lightweight ReAct loop to collect tool evidence for `request`.

    Returns a text block the caller embeds into its structured-output prompt.
    Returns empty string if no tools are configured or the gateway is down. If
    the loop hits the recursion limit, the observations gathered so far are
    salvaged rather than discarded (langgraph-agents#130).
    """
    tools = await build_mcp_tools_for_agent(agent_id)
    if not tools:
        return ""

    settings = get_settings()
    prom_uid = settings.grafana_prometheus_datasource_uid
    loki_uid = settings.grafana_loki_datasource_uid

    agent = create_react_agent(
        model=llm(agent_id, temperature=0.1),
        tools=tools,
        prompt=SystemMessage(
            content=(
                "You are a data-gathering sub-agent. Use the provided tools to "
                "collect factual observations relevant to the request. Do NOT "
                "produce analysis, recommendations, or prose — only raw tool "
                "outputs summarised as bullet points: "
                "`<tool_name>: <key finding>`. "
                "Stop after you have enough evidence to answer the request, "
                f"or after {_MAX_TOOL_CALLS} tool calls — whichever comes first.\n\n"
                "GRAFANA DATASOURCE UIDs (use exactly as shown — do NOT use "
                "the display name):\n"
                f"  Prometheus: {prom_uid}\n"
                f"  Loki:       {loki_uid}"
            )
        ),
    )

    # Stream so a recursion-limit abort still leaves us the partial state to
    # salvage. ``stream_mode="values"`` yields the full message list after each
    # super-step; the last one captured before an error is what we mine.
    last_state: dict[str, Any] = {}
    salvaged = False
    try:
        async for state in agent.astream(
            {"messages": [HumanMessage(content=f"GATHER EVIDENCE FOR:\n\n{request}")]},
            {"recursion_limit": _RECURSION_LIMIT},
            stream_mode="values",
        ):
            last_state = state
    except GraphRecursionError:
        slog.warning("evidence_gather_recursion_limit_hit", agent=agent_id)
        salvaged = True
    except Exception as exc:
        slog.warning("evidence_gather_failed", agent=agent_id, error=str(exc))
        return ""

    text = _extract_evidence(last_state.get("messages", []))
    slog.info("evidence_gathered", agent=agent_id, chars=len(text), salvaged=salvaged)
    return text
