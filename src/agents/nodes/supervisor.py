"""supervisor — fleet supervision: stuck-task escalation + rejection re-route + anomaly triage.

Calm and procedural. Doesn't take inbox entries directly; gets invoked when:
  - A specialist rejects a routed task (cascade limit = 2)
  - A task pauses past its timeout tier (handled by n8n cron sweep)
  - HolmesGPT or Prometheus surfaces an anomaly
  - Triager confidence < 0.5

Doesn't take side effects itself; routes to errand-runner for Class C+.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS, AgentId, FleetState

_AGENT_ID = "supervisor"
_MODEL = "qwen2.5:14b"
_TEMPERATURE = 0.1

_CASCADE_LIMIT = 2

Severity = Literal["page-worthy", "worth-knowing", "noise"]
Disposition = Literal["reroute", "escalate-to-user", "auto-cancel", "wait"]


class SupervisorDecision(BaseModel):
    disposition: Disposition
    new_target_agent: AgentId | None = None
    severity: Severity = "worth-knowing"
    rationale: str = Field(description="One paragraph; why this disposition.")
    user_message: str | None = Field(
        default=None,
        description="If disposition=escalate-to-user, the Tier 1/2 message text.",
    )


def _build_llm() -> BaseChatModel:
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(SupervisorDecision)  # type: ignore[return-value]


def supervisor_node(state: FleetState) -> dict[str, Any]:
    """Decide what to do with a problematic task.

    Reads `state.rejection` (set by specialist nodes), `state.cascade_count`,
    and (eventually) anomaly signals. Mutates `state.target_agent` for
    re-route, or returns an escalate-to-user output.
    """
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    rejection_summary = (
        f"rejected_by={state.rejection.rejected_by}, "
        f"reason={state.rejection.reason}, "
        f"suggested_target={state.rejection.suggested_target}, "
        f"cascade_count={state.cascade_count}"
    ) if state.rejection else "(no rejection in state)"

    # Hard cap: if we've already cascaded twice, no more reroutes; escalate.
    if state.cascade_count >= _CASCADE_LIMIT:
        return {
            "output": (
                f"supervisor: cascade limit ({_CASCADE_LIMIT}) reached for "
                f"task {state.task_id}; escalating to user."
            ),
            "target_agent": "supervisor",  # stay here; the API surface signals escalation
        }

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"TASK STATE:\n"
                f"- task_id: {state.task_id}\n"
                f"- content: {state.content[:500]}\n"
                f"- rejection: {rejection_summary}\n"
                f"- triage confidence: "
                f"{state.triage.confidence if state.triage else 'unknown'}\n\n"
                "Decide the disposition. If reroute, pick a new target_agent "
                f"from one of: {', '.join(ALL_AGENT_IDS)}. If escalate-to-user, "
                "include the user_message (concrete, one-paragraph, options "
                "if applicable). Cascade limit is 2 — we are at "
                f"{state.cascade_count}; one more reroute is OK, two more is not."
            )
        ),
    ]

    decision: SupervisorDecision = llm.invoke(messages)  # type: ignore[assignment]

    update: dict[str, Any] = {
        "output": (
            f"supervisor: {decision.disposition} "
            f"(severity={decision.severity}) — {decision.rationale[:120]}"
        ),
    }
    if decision.disposition == "reroute" and decision.new_target_agent:
        update["target_agent"] = decision.new_target_agent
        update["cascade_count"] = state.cascade_count + 1
        # Clear the rejection so the new specialist doesn't see stale state.
        update["rejection"] = None
    elif decision.disposition == "escalate-to-user" and decision.user_message:
        update["output"] = (
            f"supervisor → user (severity={decision.severity}): {decision.user_message}"
        )

    return update
