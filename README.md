# langgraph-agents

LangGraph-based multi-agent fleet for Rob's homelab. Replaces the kubeclaw / OpenClaw direction with a Python service that runs in the k8s cluster.

Design rationale + 10 locked decisions: see `project_langgraph_redesign` in claude vault.

## What's in here

17 agents as graph nodes — one Pydantic-typed `AgentId` Literal in `state.py` is the single source of truth.

| Generalists (9) | Specialists (8) |
|---|---|
| triager · reporter · note-maker · researcher · coder · errand-runner · supervisor · reviewer · doc-writer | homelab-engineer · network-operator · storage-operator · smart-home-operator · ml-operator · observability-operator · health-tracker · property-coordinator |

Personas live in the Obsidian vault at `~/vaults/claude/agents/workspaces/<agent>/{SOUL,IDENTITY,AGENTS,USER}.md` — the runtime loads them at startup from the mounted vault PVC.

Adding an agent touches **five places**. See `.agents/instructions/persona.md` ("Practical consequences").

## Graph topology

```
                                          ╭─ rejection → supervisor → reroute or → reporter → END
                                          │
START ──▶ triager ──▶ specialist ──▶ reporter ──▶ END
                       (1 of 15)            ↑
                          │                 │
                          ╰─ approval_request → errand-runner ──╯
                               (interrupt → /approval → resume)
```

Cascade depth is capped at 2 by the supervisor (`FleetState.cascade_count`). Each specialist either routes to reporter directly, sets `rejection` to bounce to supervisor, or — for class-C/D actions — returns an `ApprovalRequest` and pauses via `interrupt()` until the `/approval` endpoint resumes the workflow. **Reporter is the universal final hop** — every chain ends with reporter translating raw specialist output into a user-facing Zulip-markdown DM.

**See [`PIPELINE.md`](PIPELINE.md) for the full end-to-end walk-through** with every agent linked to its definition.

## HTTP surface

| Endpoint | Used by | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | OpenWebUI | OpenAI-compatible direct chat with one agent (model name = agent id). Bypasses fleet orchestration; still writes activity-log + emits Prom metrics. |
| `GET /v1/models` | OpenWebUI | Model picker list — one entry per agent id. |
| `POST /inbox` | Windmill `zulip-triager-webhook` (also test/voice/HolmesGPT sources) | Full fleet orchestration: triage → specialist → optional approval → execute → optional reply. |
| `POST /approval` | ntfy phone action buttons → `langgraph.${SECRET_DOMAIN}/approval` via cloudflared | Resume a paused workflow on a user verdict (approve / reject / defer). HMAC-token auth. |
| `GET /admin/tasks` | ops | In-flight tasks + checkpoint snapshots. |
| `GET /admin/asyncio-tasks` | ops | Hung-coroutine diagnostics. |
| `GET /healthz`, `/readyz` | k8s | Liveness + readiness. |
| `GET /metrics` | Prometheus ServiceMonitor | Per-agent counts / tokens / cost / duration. |

## Hardware routing

The `agents.llm.llm(agent_id)` factory chooses the model per-agent:

| Group | Endpoint | Model | Used by |
|---|---|---|---|
| `local-p40` | `ollama.ai.svc.cluster.local:11434` | `qwen2.5:7b` | triager, note-maker, errand-runner, property-coordinator, health-tracker, doc-writer |
| `local-spark` | `ollama-spark.ai.svc.cluster.local:11434` | `qwen2.5:32b` | reporter, researcher, supervisor + all 5 operator specialists |
| `local-spark-coder` | `ollama-spark.ai.svc.cluster.local:11434` | `qwen2.5-coder:32b` | coder, reviewer |
| `claude` | Anthropic API | `settings.claude_model` | None by default; opt-in per-call via `escalate=True` or when both local groups are unhealthy AND `degraded_mode_escalation_enabled=True` |

Hard constraints:
- **`health-tracker` never escalates to Claude**, regardless of the `escalate=` argument — health data stays local.
- A Spark-down request degrades to P40 (qwen2.5:7b instead of 32b) with the `effective_group` Prom label reflecting what actually served the request. If both local groups are down, the request raises `LocalOllamaUnavailable` or escalates to Claude per the flag above.

For the full routing matrix + degraded-path semantics see `.agents/instructions/hardware-routing.md`.

## Local development

```bash
# install
uv sync

# run tests (no cluster needed)
uv run pytest

# run the service against local ollama
export OLLAMA_BASE_URL=http://localhost:11434/v1
export OLLAMA_P40_URL=http://localhost:11434
export OLLAMA_SPARK_URL=http://localhost:11434
export POSTGRES_URL=postgresql://localhost:5432/langgraph_checkpoints
export MEMORY_POSTGRES_URL=postgresql://localhost:5432/langgraph_memory
export VAULT_ROOT=$HOME/vaults/claude
uv run uvicorn agents.main:app --reload --port 8765

# smoke test the triager
curl -X POST http://localhost:8765/inbox \
  -H 'content-type: application/json' \
  -d '{
    "task_id": "test-001",
    "source": "test",
    "content": "the porch light isn'\''t turning on at sunset",
    "user": "rob"
  }'
```

## Architecture

```
Laptop ────rsync────▶ sync-receiver pod ────▶ vault PVC (RWX, RO mount in app)
                                                       │
                                                       ▼
Zulip DM to Triager 📥 ────▶ Windmill `zulip-triager-webhook`
                                       │ POST /inbox
                                       ▼
                            ┌──────────────────────┐
                            │  langgraph-agents    │ ──HTTP──▶ mcp-gateway (Istio mTLS)
                            │  (FastAPI + LangGraph)│            └─▶ 14 MCP servers
                            │                      │
                            │                      │ ──OTLP──▶ langfuse-web.ai.svc:3000
                            │                      │            (per-task trace UI)
                            │  ▲ /approval (resume) │ ◀──── ntfy action buttons (HMAC)
                            │  │                   │
                            │  ▼ checkpoints       │
                            └──────────┬───────────┘
                                       │
                            ┌──────────┴──────────────────┐
                            │                             │
                  postgres-langgraph-checkpoints  postgres-langgraph-memory
                            │                             │
                  (AsyncPostgresSaver)         (MCPMemoryStore — vchordrq HNSW)
                            │
                            ▼
                  ollama (P40) + ollama-spark (GB10)
                            +
                  Anthropic API (optional escalation)

Side effects:
  - Zulip DM reply (triager-bot identity) when source=zulip
  - ntfy approval push when class-C/D action is composed
  - structlog JSON → stdout → Vector → Loki
  - Prom metrics → /metrics → ServiceMonitor
```

## Cluster deployment

Manifests live in [home-ops] under `kubernetes/apps/ai/langgraph-agents/`. The container image is built by `.github/workflows/build.yaml` and published to `ghcr.io/<owner>/langgraph-agents` with both `<semver>` and `v<semver>` tags.

[home-ops]: https://github.com/rwlove/home-ops

## Project layout

```
src/agents/
├── api/             # FastAPI route handlers (/inbox, /approval, /admin, /v1/*)
├── graphs/          # LangGraph graph definitions (fleet, approval)
├── nodes/           # one module per agent (17 nodes)
├── tools/           # mcp gateway client, activity_log writer, zulip DM, skill loader
├── state.py         # FleetState Pydantic schema (typed AgentId Literal)
├── personas.py      # vault-file loader → composed system prompts
├── llm.py           # per-agent LLM factory (P40 / Spark / Claude routing)
├── memory_store.py  # MCPMemoryStore — long-term cross-agent KG over postgres-langgraph-memory
├── observability.py # Prom metrics + structlog config + LangGraph metrics callback + Langfuse (`init_langfuse`, `langfuse_callback_handler`, `flush_langfuse`)
├── health.py        # HTTP health check used by the LLM factory's degraded routing
├── main.py          # FastAPI app + lifespan (builds checkpointer + store + graph)
└── settings.py      # pydantic-settings env config
```

## Observability

- **Prometheus** — four metrics (`langgraph_calls_total`, `langgraph_tokens_total`, `langgraph_cost_usd_total`, `langgraph_llm_duration_seconds`) labeled by `agent`, `group`, `model`, `outcome`, `trigger`. Scraped by the `langgraph-agents` ServiceMonitor.
- **Loki** — structlog JSON to stdout, picked up by Vector. Per-task field is `task_id`.
- **Langfuse** — per-task trace UI at `https://langfuse.thesteamedcrab.com` (LAN-only, Langfuse-native email/password auth). `langfuse.langchain.CallbackHandler` attaches intrinsically to every `ChatOllama` / `ChatAnthropic` built by `agents.llm.llm()` (and to the OpenWebUI surface in `api/chat_completions.py`). The pod MUST reach Langfuse via the cluster-internal Service URL `http://langfuse-web.ai.svc.cluster.local:3000` — split-horizon DNS + Cilium `toEntities: world` egress don't match the public hostname's internal LB IP, so the OTLP exporter silently times out if the public URL is used.
