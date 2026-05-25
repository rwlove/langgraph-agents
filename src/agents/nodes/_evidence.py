"""Shared evidence-gathering pre-pass for operator nodes."""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from agents.llm import llm
from agents.observability import get_logger
from agents.state import AgentId
from agents.tools.mcp_langchain import build_mcp_tools_for_agent

_RECURSION_LIMIT = 15
slog = get_logger("nodes.evidence")


async def gather_evidence(agent_id: AgentId, request: str) -> str:
    """Run a lightweight ReAct loop to collect tool evidence for `request`.

    Returns a text block the caller embeds into its structured-output prompt.
    Returns empty string if no tools are configured or the gateway is down.
    """
    tools = await build_mcp_tools_for_agent(agent_id)
    if not tools:
        return ""

    agent = create_react_agent(
        model=llm(agent_id, temperature=0.1),
        tools=tools,
        prompt=SystemMessage(content=(
            "You are a data-gathering sub-agent. Use the provided tools to "
            "collect factual observations relevant to the request. Do NOT "
            "produce analysis, recommendations, or prose — only raw tool "
            "outputs summarised as bullet points: "
            "`<tool_name>: <key finding>`. "
            "Stop after you have enough evidence to answer the request, "
            "or after 8 tool calls — whichever comes first."
        )),
    )

    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=f"GATHER EVIDENCE FOR:\n\n{request}")]},
            {"recursion_limit": _RECURSION_LIMIT},
        )
    except GraphRecursionError:
        slog.warning("evidence_gather_recursion_limit_hit", agent=agent_id)
        return ""
    except Exception as exc:
        slog.warning("evidence_gather_failed", agent=agent_id, error=str(exc))
        return ""

    messages = result.get("messages", [])
    if not messages:
        return ""

    last = messages[-1]
    text = str(getattr(last, "content", "")).strip()
    slog.info("evidence_gathered", agent=agent_id, chars=len(text))
    return text
