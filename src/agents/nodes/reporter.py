"""reporter — final-hop user-facing messenger.

Every task chain terminates here. Reporter takes the chain's raw output
(specialist work product, often including vault paths and raw URLs) and
translates it into a Zulip-flavored markdown message the user can act on.

Replaces the upstream specialist's `output` in FleetState — the
completion_post webhook then emits reporter's rendering verbatim plus a
small meta footer.

This is a NEW agent introduced as part of the reporter→historian rename;
the daily-digest agent that used to live at this module path is now
`historian` (see PR #76).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm import llm
from agents.observability import get_logger
from agents.personas import load_persona
from agents.state import AgentId, FleetState

_AGENT_ID: AgentId = "reporter"
_TEMPERATURE = 0.3
_MAX_CONTENT_CHARS = 2000
_MAX_OUTPUT_CHARS = 3000

slog = get_logger("nodes.reporter")


def reporter_node(state: FleetState) -> dict[str, Any]:
    """Render the chain's terminal state into a user-facing Zulip-markdown message.

    Replaces ``state.output`` with the rendered text. The completion-post
    webhook reads ``state.output`` after the graph completes, so this is the
    user's final view of the chain.
    """
    persona = load_persona(_AGENT_ID)

    upstream_agent = state.target_agent or "triager"
    upstream_output = (state.output or "").strip()

    # Special case: reporter targeted directly (target_agent="reporter" via the
    # /inbox envelope). No specialist ran; treat state.content as the input.
    if upstream_agent == "reporter" and not upstream_output:
        upstream_output = state.content or ""
        upstream_agent = "(direct)"

    # Special case: nothing actionable to render and no rejection/approval to
    # surface. Emit a pointer rather than a fabricated message.
    has_anything = bool(
        upstream_output or state.rejection is not None or state.approval_request is not None
    )
    if not has_anything:
        slog.info("reporter_no_input", upstream_agent=upstream_agent)
        return {
            "output": (
                f"**No output** from `{upstream_agent}` — "
                f"see `hai task show {state.task_id}` for details."
            )
        }

    materials: list[str] = []
    if state.content:
        materials.append(f"## Original ask\n{state.content[:_MAX_CONTENT_CHARS]}")
    if upstream_output:
        materials.append(
            f"## Specialist output (from `{upstream_agent}`)\n{upstream_output[:_MAX_OUTPUT_CHARS]}"
        )
    if state.rejection is not None:
        materials.append(f"## Rejection\n{state.rejection}")
    if state.approval_request is not None:
        materials.append(f"## Approval request\n{state.approval_request}")
    materials.append(f"## Data tier\n{state.data_tier}")

    ask = "\n\n".join(materials) + (
        "\n\n## Your task\n"
        "Render this into a Zulip-markdown DM per your SOUL.\n"
        "- Convert any vault paths into "
        "`obsidian://open?vault=claude&file=<url-encoded-path>` deep links.\n"
        "- Convert raw URLs into `[labeled](url)` markdown links.\n"
        "- If the specialist's output is already clear and user-facing, pass it through "
        "with minimal formatting — do not paraphrase or add bloat.\n"
        "- Be terse. Lead with the answer."
    )

    try:
        response = llm(_AGENT_ID, temperature=_TEMPERATURE).invoke(
            [SystemMessage(content=persona), HumanMessage(content=ask)]
        )
        rendered = str(response.content).strip()
    except Exception as exc:
        # LLM failure → emit the upstream output unchanged so the user sees
        # *something*. Better than swallowing the work entirely.
        slog.warning(
            "reporter_llm_failed",
            error_type=type(exc).__name__,
            upstream_agent=upstream_agent,
            fallback="raw_upstream",
        )
        return {"output": upstream_output or "_(reporter unavailable; no fallback content)_"}

    slog.info(
        "reporter_rendered",
        upstream_agent=upstream_agent,
        in_chars=len(upstream_output),
        out_chars=len(rendered),
    )
    return {"output": rendered}
