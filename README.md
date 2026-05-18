# langgraph-agents

LangGraph-based multi-agent fleet for Rob's homelab. Replaces the kubeclaw/OpenClaw direction with a Python service that runs in the k8s cluster.

Design rationale + 10 locked decisions: see `project_langgraph_redesign` in claude vault.

## What's in here

17 agents as graph nodes:

| Generalists | Specialists |
|---|---|
| triager · reporter · note-maker · researcher · coder · errand-runner · supervisor · reviewer · doc-writer | homelab-engineer · network-operator · storage-operator · smart-home-operator · ml-operator · observability-operator · health-tracker · property-coordinator |

Personas live in the Obsidian vault at `~/vaults/claude/agents/workspaces/<agent>/{SOUL,IDENTITY,AGENTS,USER}.md` — the runtime loads them at startup.

## HTTP surface

| Endpoint | Used by | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | OpenWebUI | OpenAI-compatible chat with one agent (model name = agent id) |
| `POST /inbox` | n8n inbox webhook | Full fleet orchestration: triage → specialist → approval → execute |
| `POST /approval` | n8n approval-broker | Resume a paused workflow on Zulip reaction (👍 / 👎 / ⏸️) |
| `GET /admin/tasks` | ops | In-flight tasks + checkpoints |
| `GET /healthz`, `/readyz` | k8s | Liveness + readiness |

## Local development

```bash
# install
uv sync

# run tests (no cluster needed)
uv run pytest

# run the service against local ollama
export OLLAMA_BASE_URL=http://localhost:11434/v1
export POSTGRES_URL=postgresql://localhost:5432/langgraph_checkpoints
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
Vault (laptop) ──rsync──▶ sync-receiver pod ──▶ vault PVC (RWX)
                                                       │ RO mount
                                                       ▼
n8n /inbox ────▶ langgraph-agents ──HTTP──▶ mcp-gateway (14 servers)
                       │
              Postgres │  checkpoints + pgvector memory
                       │
              Langfuse │  per-task traces + cost
                       │
                 Zulip │  agent posts + approval reactions
                       │
              Pushover │  Tier 1 escalation
```

## Cluster deployment

Manifests live in [home-ops] under `kubernetes/apps/ai/langgraph-agents/`. The container image is built by `.github/workflows/build.yaml` and published to `ghcr.io/<owner>/langgraph-agents`.

[home-ops]: https://github.com/rwlove/home-ops

## Project layout

```
src/agents/
├── graphs/         # LangGraph graph definitions (fleet, approval, rejection, awaiting)
├── nodes/          # one module per agent
├── tools/          # mcp gateway client, skill loader, vault helpers
├── api/            # FastAPI routes
├── state.py        # GraphState Pydantic schema (typed routing targets)
├── personas.py     # vault-file loader → composed system prompts
├── memory.py       # pgvector retrieval helpers
├── checkpointer.py # Postgres checkpointer wiring
├── tracing.py      # Langfuse integration
└── settings.py     # env config
```
