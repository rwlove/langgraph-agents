# Hardware routing — per-agent LLM selection

The `agents.llm.llm(agent_id)` factory is the single source of truth for which model serves which agent on which GPU. This doc summarizes the live mapping so changes to `AGENT_GROUP` / `GROUP_MODELS` don't drift from the README and so Claude sessions in this repo can reason about the routing without re-reading the code each time.

## Groups

| Group | Endpoint | Model | Hardware |
|---|---|---|---|
| `local-p40` | `http://ollama.ai.svc.cluster.local:11434` | `qwen2.5:7b` | P40 (24 GiB VRAM, Pascal era) — ≤8b model cap per `project_p40_model_size_cap` |
| `local-spark` | `http://ollama-spark.ai.svc.cluster.local:11434` | `qwen2.5:32b` | NVIDIA Spark / GB10 (Grace-Blackwell) — DCGM counters partly broken; see `reference_dcgm_gb10_broken_counters` |
| `claude` | Anthropic API | `settings.claude_model` (default `claude-opus-4-7`) | Off-cluster |

## Per-agent assignment (`AGENT_GROUP`)

| Agent | Group | Rationale |
|---|---|---|
| triager | local-p40 | Mechanical routing decision; structured-output friendly with 7b |
| note-maker | local-p40 | Short drafts, low reasoning load |
| errand-runner | local-p40 | Tool-call mechanics; reasoning is in the upstream specialist |
| property-coordinator | local-p40 | Short structured updates |
| health-tracker | local-p40 | **Hard-pinned local**: health data never leaves the cluster, even on `escalate=True` |
| doc-writer | local-p40 | Mostly templated formatting |
| reporter | local-spark | Aggregates activity logs; needs context length + reasoning |
| researcher | local-spark | Multi-step reasoning + tool use |
| supervisor | local-spark | Cascade-decision reasoning |
| coder | local-spark | Code generation + critique |
| reviewer | local-spark | Code-review reasoning |
| homelab-engineer | local-spark | Cross-domain reasoning over k8s/Flux/infra |
| network-operator | local-spark | Policy + topology reasoning |
| storage-operator | local-spark | Multi-backend tradeoff reasoning |
| smart-home-operator | local-spark | HA YAML synthesis + automation tradeoffs |
| ml-operator | local-spark | Model lifecycle + GPU planning |
| observability-operator | local-spark | PromQL + alert routing reasoning |

## Routing rules (factory behavior)

1. **`health-tracker` is local-only.** Explicit `escalate=True` or `group_override="claude"` is silently downgraded to `local-p40`.
2. **`escalate=True` + `ANTHROPIC_API_KEY` present** → Claude, regardless of `AGENT_GROUP`.
3. **`group == "local-spark"`**:
   - Spark healthy → Spark + qwen2.5:32b.
   - Spark unhealthy, P40 healthy → degrade to P40 + qwen2.5:7b (logged; metric `effective_group=local-p40` reflects what served).
   - Both down, `degraded_mode_escalation_enabled=True`, key present → Claude.
   - Both down, escalation disabled → raise `LocalOllamaUnavailable`. `/inbox` catches and queues the task for retry (see `agents.queue`).
4. **`group == "local-p40"`**:
   - P40 healthy → P40 + qwen2.5:7b.
   - P40 unhealthy, degraded-escalation on, key present → Claude.
   - P40 unhealthy, escalation off → raise `LocalOllamaUnavailable`.
5. **`group == "claude"` (explicit)** — requires `ANTHROPIC_API_KEY`; otherwise raises.

## Implications for changes

- **Adding an agent** — assign it to one of the two local groups in `AGENT_GROUP` (claude is opt-in only). Light/mechanical → P40; reasoning/structured-output → Spark.
- **Bumping a group's model** — edit `GROUP_MODELS` (one line). The metric `model` label updates automatically.
- **Routing an agent off Spark** — don't fall back to a third group; either P40 or Claude, no in-between. P40's qwen2.5:7b is "degraded but serving"; anything bigger doesn't fit on P40.
- **Health-tracker exception is permanent** — never weaken the `health-tracker` local-only branch in `llm.py` without an explicit user instruction.

## Side-effect: chat_completions has its own routing today

`api/chat_completions.py` (the OpenWebUI surface) uses a hardcoded `_PER_AGENT_MODEL` dict and sends every request to `settings.ollama_base_url` (NOT the per-agent Spark/P40 endpoint). It bypasses `agents.llm.llm()` entirely.

Consequence: OpenWebUI Spark agents currently run on P40 with the 7b/32b choice made locally by `_PER_AGENT_MODEL`, not by `AGENT_GROUP`. The metrics callback was wired in `chat_completions._make_llm()` (PR #26) so observability is intact, but the routing is duplicated and will drift. Folding `chat_completions._make_llm()` to call `agents.llm.llm(agent_id)` is a small refactor and is tracked as a follow-up — not done in this doc PR to keep scope clean.
