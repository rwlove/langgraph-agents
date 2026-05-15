"""property-coordinator — 3532 Foxhall contractor + tier-sequenced fixes.

Calendar-aware. Tracks Custom Works (deck), Goudy (pool), Joakim (handyman)
and historical vendors. Drafts contractor communications + tier-1..4 fix
plans. Never sends external messages directly; always drafts for Rob.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ActionClass, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID = "property-coordinator"
_MODEL = "qwen2.5:7b"
_TEMPERATURE = 0.2

Tier = Literal["1", "2", "3", "4"]


class PropertyAction(BaseModel):
    description: str
    tier: Tier = Field(
        description=(
            "1=safety-critical, 2=this-season, 3=opportunistic, 4=nice-to-have"
        )
    )
    blockers: list[str] = Field(default_factory=list)
    vendor: str | None = Field(
        default=None,
        description="Vendor handle, first-name-only or company.",
    )
    cost_estimate_usd: float | None = None


class PropertyPlan(BaseModel):
    summary: str = Field(description="What needs doing at 3532 Foxhall.")
    actions: list[PropertyAction]
    while_youre_here: list[str] = Field(
        default_factory=list,
        description="Items that share crew/time/equipment with another action.",
    )
    seasonal_lead_time: list[str] = Field(
        default_factory=list,
        description="Items where lead time matters (2-4 weeks ahead).",
    )
    action_class: ActionClass = "B"  # default to "draft only"
    handoff_target: Literal["user", "note-maker", "smart-home-engineer", "errand-runner"] = "user"


def _build_llm() -> BaseChatModel:
    settings = get_settings()
    return ChatOllama(
        model=_MODEL,
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=_TEMPERATURE,
    ).with_structured_output(PropertyPlan)  # type: ignore[return-value]


def _render_markdown(plan: PropertyPlan, task_id: str) -> str:
    def _action(a: PropertyAction) -> str:
        vendor = f" ({a.vendor})" if a.vendor else ""
        cost = f" — ~${a.cost_estimate_usd:.0f}" if a.cost_estimate_usd else ""
        blockers = (
            f"\n  Blocked on: {', '.join(a.blockers)}" if a.blockers else ""
        )
        return f"- **Tier {a.tier}** — {a.description}{vendor}{cost}{blockers}"

    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "_(none)_"

    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: property-plan\n"
        f"action_class: {plan.action_class}\n"
        f"handoff_target: {plan.handoff_target}\n"
        "---\n\n"
        "# Property plan — 3532 Foxhall\n\n"
        f"## Summary\n\n{plan.summary}\n\n"
        "## Actions (tiered)\n\n"
        + "\n".join(_action(a) for a in plan.actions)
        + "\n\n## While-you're-here\n\n"
        f"{_bullets(plan.while_youre_here)}\n\n"
        "## Seasonal lead time\n\n"
        f"{_bullets(plan.seasonal_lead_time)}\n"
    )


def property_coordinator_node(state: FleetState) -> dict[str, Any]:
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    triage_hint = ""
    if state.triage:
        triage_hint = f"\n\nTRIAGE CONTEXT:\n- summary: {state.triage.summary}\n"

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}{triage_hint}\n\n"
                "Produce a PropertyPlan. Tier 1-4. Surface explicit blockers "
                "(quote, permit, weather window, Rob's decision). When a "
                "contractor will be on-site, populate while_youre_here. "
                "Never send external messages; draft only."
            )
        ),
    ]

    plan: PropertyPlan = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(plan, state.task_id)
    result = write_draft(state.task_id, markdown, kind="property")

    return {
        "output": (
            f"property plan: {result.path} "
            f"(actions={len(plan.actions)}, while_youre_here={len(plan.while_youre_here)})"
        ),
    }
