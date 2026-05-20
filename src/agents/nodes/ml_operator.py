"""ml-operator — local inference, GPU substrate, langgraph-agents framework.

Owns Ollama / HolmesGPT / Open WebUI tool curation / langgraph-agents
framework + deployment / immich-pet-tagger fork / Immich CLIP awareness /
Frigate+ retraining loop / GPU resource matrix / Spark migration planning.
Propose-first by default. Prime directive: this agent **cannot crash the
inference path**. Class C+ side effects route through `errand-runner` with
a signed approval token. The eight-clause execution gate (read-back · GPU
headroom · failure mode named · verbatim rollback · no Spark-gated flip ·
no mid-flight pipeline interruption · no bulk apply · positive verification)
governs what may even be *proposed* as Class C.

Quantitative voice: every recommendation has VRAM cost + precision/recall
+ latency + $/inference. One knob at a time. Define measurement baseline
before rollout.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.llm import llm
from agents.personas import load_persona
from agents.state import ActionClass, AgentId, ApprovalRequest, FleetState
from agents.tools.obsidian import write_draft

_AGENT_ID: AgentId = "ml-operator"
_TEMPERATURE = 0.2

# Canonical write target for ml-operator-proposed actions. Most ML side
# effects (helmrelease resource bump, Ollama model pull via spec change,
# Open WebUI tool toggle in a ConfigMap, langgraph-agents env flip) land
# as a kubectl apply against home-ops. We don't try to thread ollama-mcp
# pulls or Open WebUI's HTTP admin API through here — those are follow-ups
# that need their own errand-runner allowlist entries and signed-token
# review. Mirrors smart_home_operator._HA_WRITE_TARGET.
_K8S_WRITE_TARGET = "kubectl-mcp.kubectl_apply"

# action_class values that imply a side effect requiring approval. Class A
# (read-only analysis) and B (vault-local commit) never need a user verdict;
# Class C (push/rollout) and D (apply directly) do.
_APPROVAL_REQUIRED_CLASSES: frozenset[ActionClass] = frozenset({"C", "D"})

Knob = Literal[
    "model",
    "detection_threshold",
    "zone",
    "mask",
    "embedding_model",
    "keep_alive",
    "concurrent_load",
    "agent_set",
    "tool_registration",
    "helmrelease_resources",
    "other",
]


class MLFinding(BaseModel):
    """Structured output for an ml-operator pass.

    Mirrors the eight-clause execution gate from IDENTITY.md. The
    spark_gated flag forces user handoff for activation flips that
    must wait until Spark is primary.
    """

    summary: str = Field(description="One-sentence what + why.")
    failure_domain: str = Field(
        description=(
            "What inference path silently degrades if this change is wrong. "
            "Ollama crashloop? Open WebUI chats broken? HolmesGPT triage "
            "offline? langgraph-agents version skew? CLIP index corruption?"
        )
    )
    current_baseline: str = Field(
        description=(
            "Current metric + VRAM cost. Specific: '0.18 FP/cam/day at "
            "threshold 0.65, qwen2.5:7b resident at 5.1 GiB on worker8.'"
        )
    )
    hypothesis: str = Field(description="What change is being proposed + expected delta.")
    knob: Knob
    knob_change: str = Field(description="From → to.")
    vram_delta_gib: float = Field(
        description="Change in resident VRAM. Negative = freeing. Zero if not a model swap."
    )
    gpu_headroom_check: str = Field(
        default="N/A",
        description=(
            "For Ollama pulls / pod scheduling: VRAM headroom confirmed "
            "enough for the new model AND any models that should stay "
            "resident. N/A for non-GPU changes."
        ),
    )
    measure_after: str = Field(
        description="How + when to measure outcome (e.g. '24h Frigate FP rate per camera')."
    )
    blast_radius: str = Field(
        description=(
            "Workloads that share the GPU / consume the model / depend on "
            "the agent set. 'Just immich-ml' is fine if that's true; "
            "enumerate consumers when it isn't."
        )
    )
    rollback: str = Field(
        description=(
            "Mechanical rollback. Previous version / agent set / tool list "
            "verbatim. Restoring is paste-and-restart, not re-download. If "
            "rollback requires re-indexing / re-training, the gate is NOT "
            "satisfied — set action_class: A instead."
        )
    )
    recovery_path_touched: bool = Field(
        default=False,
        description=(
            "True if the change is Spark-gated (ENABLE_CLAUDE_API flip, "
            ">8b model on P40, immich-pet-tagger upstream switch, "
            "langgraph production activation, HolmesGPT timeout revert) or "
            "mid-flight pipeline (Immich CLIP backlog, pet-tagger "
            "mid-run). If true, handoff defaults to user."
        ),
    )
    spark_gated: bool = Field(
        default=False,
        description="True if this is queued for post-Spark-arrival.",
    )
    affects_production: bool = Field(
        default=False,
        description="True if this changes inference behavior users can observe.",
    )
    action_class: ActionClass = Field(
        description=(
            "A=read-only, B=local commit / vault draft, C=push/rollout via "
            "errand-runner (≤8b Ollama pull, single resource bump, single "
            "tool add), D=apply directly. D should be exceedingly rare."
        )
    )
    handoff_target: Literal[
        "user", "errand-runner", "homelab-engineer", "storage-operator", "smart-home-operator"
    ] = "user"
    affected_resources: list[str] = Field(
        default_factory=list,
        description=(
            "Model names, pod names, helmrelease names, Open WebUI tool "
            "IDs, langgraph AgentIds, Frigate camera names."
        ),
    )
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Vault paths, memory entries, prom queries cited, model card URLs, github issues."
        ),
    )


def _build_llm() -> BaseChatModel:
    return llm(_AGENT_ID, temperature=_TEMPERATURE).with_structured_output(MLFinding)  # type: ignore[return-value]


def _render_markdown(f: MLFinding, task_id: str) -> str:
    resources = "\n".join(f"- `{r}`" for r in f.affected_resources) or "_(none)_"
    refs = "\n".join(f"- {r}" for r in f.references) or "_(none)_"
    warnings = ""
    if f.recovery_path_touched:
        warnings += (
            "\n> ⚠️ Recovery path touched: Spark-gated activation flip "
            "or mid-flight pipeline. Defaults to user handoff regardless "
            "of action_class.\n"
        )
    if f.spark_gated:
        warnings += (
            "\n> ⏳ Spark-gated: queued for post-Spark-arrival per "
            "project_langgraph_activation_gated_on_spark / P40 model cap.\n"
        )
    return (
        "---\n"
        f"task_id: {task_id}\n"
        "kind: ml-finding\n"
        f"action_class: {f.action_class}\n"
        f"handoff_target: {f.handoff_target}\n"
        f"recovery_path_touched: {f.recovery_path_touched}\n"
        f"spark_gated: {f.spark_gated}\n"
        f"affects_production: {f.affects_production}\n"
        f"knob: {f.knob}\n"
        f"vram_delta_gib: {f.vram_delta_gib}\n"
        "---\n\n"
        f"# ML finding — {f.summary[:60]}\n\n"
        f"{warnings}"
        f"**Summary:** {f.summary}\n\n"
        f"**Knob:** {f.knob} — {f.knob_change}\n"
        f"**VRAM delta:** {f.vram_delta_gib:+.2f} GiB\n\n"
        "## Failure domain\n\n"
        f"{f.failure_domain}\n\n"
        "## Baseline\n\n"
        f"{f.current_baseline}\n\n"
        "## Hypothesis\n\n"
        f"{f.hypothesis}\n\n"
        "## GPU headroom check\n\n"
        f"{f.gpu_headroom_check}\n\n"
        "## Measurement plan\n\n"
        f"{f.measure_after}\n\n"
        "## Blast radius\n\n"
        f"{f.blast_radius}\n\n"
        "## Rollback (verbatim)\n\n"
        f"```\n{f.rollback}\n```\n\n"
        "## Affected resources\n\n"
        f"{resources}\n\n"
        "## References\n\n"
        f"{refs}\n"
    )


def ml_operator_node(state: FleetState) -> dict[str, Any]:
    """Analyze + propose for any ML / inference request.

    Prime-directive enforcement is in the persona; the schema makes the
    gate fields mandatory so a downstream agent can machine-verify the
    safety analysis before acting.
    """
    persona = load_persona(_AGENT_ID)
    llm = _build_llm()

    triage_hint = ""
    if state.triage:
        triage_hint = (
            f"\n\nTRIAGE CONTEXT:\n- domain: {state.triage.domain}\n"
            f"- intent: {state.triage.intent}\n- mode: {state.triage.mode}\n"
            f"- summary: {state.triage.summary}\n"
        )

    messages = [
        SystemMessage(content=persona),
        HumanMessage(
            content=(
                f"REQUEST:\n\n{state.content}{triage_hint}\n\n"
                "Produce an MLFinding. Prime directive: you cannot crash "
                "the inference path. Quantitative — VRAM cost, expected "
                "precision/recall change, $/inference if relevant. Move "
                "ONE knob at a time. Define a measurement window before "
                "rollout. If this is a Spark-gated decision (ENABLE_CLAUDE_API "
                "flip, >8b model on P40, immich-pet-tagger upstream switch, "
                "langgraph production activation, HolmesGPT timeout revert), "
                "set spark_gated=true AND recovery_path_touched=true AND "
                "action_class=A. Verbatim rollback. Run the eight-clause "
                "execution gate before choosing action_class C."
            )
        ),
    ]

    finding: MLFinding = llm.invoke(messages)  # type: ignore[assignment]
    markdown = _render_markdown(finding, state.task_id)
    result = write_draft(state.task_id, markdown, kind="ml")

    update: dict[str, Any] = {
        "output": (
            f"ml finding: {result.path} "
            f"(knob={finding.knob}, vram_delta={finding.vram_delta_gib:+.2f}GiB, "
            f"class={finding.action_class}, handoff={finding.handoff_target}, "
            f"spark_gated={finding.spark_gated})"
        ),
    }

    # Approval composition. The schema's `handoff_target == "errand-runner"`
    # + `action_class in {C, D}` is the explicit signal the LLM emits for "I
    # want this ML side effect executed, please get the user's verdict."
    # Spark-gated / recovery-path overrides force `user` handoff in the
    # persona, so by the time we get here those tasks have already been
    # downgraded out of the errand-runner path.
    if (
        finding.handoff_target == "errand-runner"
        and finding.action_class in _APPROVAL_REQUIRED_CLASSES
    ):
        update["approval_request"] = _compose_approval_request(finding)
        # Specialist → errand-runner routing. The fleet graph's
        # `_route_after_specialist` reads this; without it the graph would
        # END before the errand-runner ever sees the request.
        update["target_agent"] = "errand-runner"

    return update


def _compose_approval_request(finding: MLFinding) -> ApprovalRequest:
    """Translate an MLFinding into the ApprovalRequest errand-runner expects.

    Class C requires an undo path (errand-runner refuses Class C without one
    and escalates to D). For ML changes we derive the undo by taking the
    finding's rollback text — verbatim — and prefixing it with the canonical
    write target. The rollback field is already mandatory + verbatim per the
    eight-clause execution gate; if a rollback is "re-index / re-train" the
    gate flips action_class to A in the persona, so we never get here
    without a paste-and-restart rollback.

    payload_summary collapses the proposed knob change into the broker UI's
    single-line render. Truncated at 200 chars to keep Pushover / Zulip
    notification surfaces readable; the full draft lives in the vault and
    is linked from the broker message.
    """
    summary = (f"{finding.knob}: {finding.knob_change} (VRAM {finding.vram_delta_gib:+.2f} GiB)")[
        :200
    ]
    undo_path: str | None = None
    if finding.rollback.strip():
        # Embed the rollback as a description, not a callable target — the
        # broker UI displays it; errand-runner uses its presence (not its
        # content) as the Class-C gate.
        undo_path = f"{_K8S_WRITE_TARGET}: {finding.rollback.strip().splitlines()[0][:160]}"
    return ApprovalRequest(
        action_class=finding.action_class,
        target=_K8S_WRITE_TARGET,
        payload_summary=summary,
        undo_path=undo_path,
        proposed_by=_AGENT_ID,
    )
