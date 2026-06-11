# Hardware routing — per-agent LLM selection

The `agents.llm.llm(agent_id)` factory is the single source of truth for which model serves which agent on which GPU. This doc summarizes the live mapping so changes to `AGENT_GROUP` / `GROUP_MODELS` don't drift from the README and so Claude sessions in this repo can reason about the routing without re-reading the code each time.

## Hardware facts — GB10 Spark (ASUS Ascent GX10)

Sourced from NVIDIA/ASUS spec sheets (2026-06) + measured on the live unit.
Don't assume beyond these; re-verify before sizing model work.

| Fact | Value | Notes |
|---|---|---|
| Unified memory | 128 GB LPDDR5x, 256-bit | CPU+GPU coherent |
| Memory bandwidth | **273 GB/s** (NVIDIA GB10) / **276 GB/s** (ASUS GX10) | the binding constraint for token generation |
| Inter-unit link | **NVIDIA ConnectX-7, 200 Gbps (= 25 GB/s)** + a 10 GbE LAN | GX10 has one ConnectX-7 (Founders ed. has dual QSFP) |
| Two-unit config | direct ConnectX-7 cable, no switch → **256 GB pool → models up to 405B** | NVIDIA's official ceiling |
| Measured generation | **qwen3-next:80b-a3b ≈ 62 tok/s** (current local-spark model), qwen2.5:32b ≈ 10 tok/s (prev, dense), qwen2.5:72b ≈ 5 tok/s | MoE (3B active) is why the bigger model is also ~6× faster; prompt eval ~240 tok/s |
| Cold-load | **qwen3-next ≈ 8.6 min** (50 GB off ceph + novel-arch init) | the reason `OLLAMA_LOAD_TIMEOUT=20m`; a restart/eviction stalls the reasoning tier >8 min — keep-warm (`MAX_LOADED=5`) makes it rare |
| DCGM | mostly broken (power + SM clock only) | use power draw as the "busy" proxy; see `reference_dcgm_gb10_broken_counters` |

**Memory ≫ link (276 ÷ 25 ≈ 11×).** Splitting a model across two Sparks buys
*capacity* (fit a 235B/405B model), not *speed* — cross-device traffic rides the
25 GB/s link while each Spark's own weights stream at ~273 GB/s. So a 2nd Spark
is best run **independent** (one serving the fleet hot, one dedicated to a big
always-warm model), not **linked** for one giant slow model. Full bake-off +
rationale: `reference_gb10_local_model_bakeoff` in home-ops memory.

**Model selection on this hardware:** bigger *dense* doesn't help vs a strong
remote model (32b ≈ 72b in quality, half the speed); **MoE is the lever**
(gpt-oss:120b, ~5B active, fits and is far better per token). Multi-node serving
(linked Sparks) needs vLLM/SGLang — ollama is single-node.

## Groups

| Group | Endpoint | Model | Hardware |
|---|---|---|---|
| `local-p40` | `http://ollama.ai.svc.cluster.local:11434` | `qwen2.5:7b` | P40 (24 GiB VRAM, Pascal era) — ≤8b model cap per `project_p40_model_size_cap` |
| `local-spark` | `http://ollama-spark.ai.svc.cluster.local:11434` | `qwen3-next:80b-a3b-instruct-q4_K_M` | NVIDIA Spark / GB10. MoE (80B/3B active) — bigger + ~6× faster than the prev qwen2.5:32b, but ~8.6 min cold-load. DCGM counters partly broken; see `reference_dcgm_gb10_broken_counters` |
| `local-spark-coder` | `http://ollama-spark.ai.svc.cluster.local:11434` | `qwen2.5-coder:32b` | Same Spark / GB10 instance as `local-spark`. Coder-tuned model for code-focused agents; Spark's `OLLAMA_MAX_LOADED_MODELS=3` lets both 32b models sit resident |
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
| coder | local-spark-coder | Code generation + critique on coder-tuned 32b |
| reviewer | local-spark-coder | Code-review reasoning on coder-tuned 32b |
| homelab-engineer | local-spark | Cross-domain reasoning over k8s/Flux/infra |
| network-operator | local-spark | Policy + topology reasoning |
| storage-operator | local-spark | Multi-backend tradeoff reasoning |
| smart-home-operator | local-spark | HA YAML synthesis + automation tradeoffs |
| ml-operator | local-spark | Model lifecycle + GPU planning |
| observability-operator | local-spark | PromQL + alert routing reasoning |

## Routing rules (factory behavior)

1. **`health-tracker` is local-only.** Explicit `escalate=True` or `group_override="claude"` is silently downgraded to `local-p40`.
2. **`escalate=True` + `ANTHROPIC_API_KEY` present** → Claude, regardless of `AGENT_GROUP`.
3. **`group == "local-spark"` or `group == "local-spark-coder"`** (same Spark instance, different model):
   - Spark healthy → Spark + the group's model (general `qwen3-next:80b-a3b-instruct-q4_K_M` or `qwen2.5-coder:32b`).
   - Spark unhealthy, P40 healthy → degrade to P40 + qwen2.5:7b (logged; metric `effective_group=local-p40` reflects what served). Coder requests degrade to the same general 7b — weak at code, but the request doesn't fail.
   - Both down, `degraded_mode_escalation_enabled=True`, key present → Claude.
   - Both down, escalation disabled → raise `LocalOllamaUnavailable` with `failed_group` equal to the originally-requested group. `/inbox` catches and queues the task for retry (see `agents.queue`).
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

## chat_completions delegates to the factory

`api/chat_completions.py` (the OpenWebUI surface) calls `agents.llm.llm(agent_id, trigger="openwebui")` for every request. Same per-agent Spark/P40 routing as the /inbox + scheduled-graph paths; reasoning agents (reporter, researcher, supervisor, the five operator agents) get qwen3-next:80b-a3b-instruct-q4_K_M on Spark, code agents (coder, reviewer) get qwen2.5-coder:32b on the same Spark endpoint, light agents get qwen2.5:7b on P40.

The `trigger="openwebui"` propagates to `LangGraphMetricsCallback`'s `trigger` label on `langgraph_calls_total` so Grafana panels filtering on that label keep working. The Langfuse callback also lands on OpenWebUI chats now (same factory attachment), so per-task traces show up alongside /inbox runs in the trace UI.
