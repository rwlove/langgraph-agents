# Pipeline — end to end

How a task flows through HomeAIOps from ingress to user notification, with every agent in the chain linked to its `SOUL.md`.

This is the operator's view of the system. For the implementation details (graph wiring, node code, routing internals), see `src/agents/graphs/fleet.py` + `src/agents/nodes/*.py`. For the rationale behind individual agents, click through to each agent's SOUL below.

---

## At a glance

```
                                          ╭─ rejection → supervisor → reroute or → reporter → END
                                          │
INPUT  ──▶  /inbox  ──▶  triager  ──▶  specialist  ──▶  reporter  ──▶  END
  ↑                        │            (1 of 15)                ↓
  │                        │                  │           completion-post webhook
external                   │                  ╰─ approval_request → errand-runner ──▶ reporter
inputs                     │                                          │
(CLI, Zulip,               │                                          │
 webhooks,                 ╰─ target_agent pin                       (interrupt → user → /approval → resume)
 cron, etc.)                 (skip triage)
```

Every chain terminates at [reporter](agents/workspaces/reporter/SOUL.md) — the universal final-hop messenger. Specialists do the domain work; reporter is the single voice that talks to the user.

---

## 1. Ingress — `POST /inbox`

External callers submit tasks to `POST /inbox` with the envelope defined in [`src/agents/api/inbox.py`](src/agents/api/inbox.py). The contract is consistent across every input source:

| Field | Required | Meaning |
|---|---|---|
| `task_id` | yes | Advisory — the queue assigns a ULID; this is logged but not canonical |
| `source` | yes | Discriminator: `cli`, `zulip`, `voice`, `holmesgpt`, `openwebui`, `test` |
| `content` | yes | The task's actual prompt |
| `user` | no (default `"rob"`) | Who's asking |
| `target_agent` | no | **Pin to a specific specialist, skipping the triager** (lga 0.2.40+) |
| `requester` | no | `rob` / `renee` / `system` — gates Renee's allowlist |
| `priority` | no (default `"normal"`) | `low` / `normal` / `high` / `urgent` |
| `data_tier` | no (default `"internal"`) | `public` / `internal` / `restricted` — drives reporter redaction |
| `idempotency_key` | no | Dedup key — duplicate keys return the prior task_id |

`POST /inbox` returns **202 + task_id** immediately; the queue worker drains async. Poll `GET /admin/tasks/<id>` for terminal state.

### Inputs that feed `/inbox` today

All home-ops Windmill workflows under `kubernetes/apps/home/windmill/workflows/`:

| Workflow | Trigger | `source` | `target_agent` pin |
|---|---|---|---|
| `langgraph-inbox` | direct UI/API | `cli` | — |
| `langgraph-renovate-triage` | hourly cron | `cli` | [researcher](agents/workspaces/researcher/SOUL.md) |
| `alertmanager-holmesgpt-notify` | AlertManager webhook | `cli` | per-namespace specialist |
| `zulip-triager-webhook` | Zulip @-mention / DM | `zulip` | — |
| `langgraph-daily-digest` | daily cron | `cli` | [historian](agents/workspaces/historian/SOUL.md) |
| `langgraph-awaiting-user-sweep` | hourly cron | `cli` | — |
| `langgraph-cost-cap-watcher` | hourly cron | `cli` | — |
| `langgraph-dlq-watcher` | hourly cron | `cli` | — |
| `smart-home-intent-drift` | weekly cron | `cli` | [smart-home-operator](agents/workspaces/smart-home-operator/SOUL.md) |
| `hai task add` (CLI direct) | ADMIN runs the CLI | `cli` | optional |

---

## 2. Triage — [triager](agents/workspaces/triager/SOUL.md)

The triager runs on `local-p40` (qwen2.5:7b — fast classifier). Its only job is to read `state.content` and produce a [`TriageDecision`](src/agents/state.py): `{summary, domain, intent, target_agent, confidence, reasoning}`.

**Short-circuit**: if the caller already set `target_agent` in the envelope, the triager skips its own routing and emits a synthetic decision (`reasoning: "caller-pinned via envelope.target_agent — triager skipped"`). This bypasses the small model's known mis-routing on triage-shaped prompts.

If the triager mis-classifies or sets a `target_agent` it doesn't recognize, the graph routes to [reporter](agents/workspaces/reporter/SOUL.md) directly (rendering the error honestly).

---

## 3. Specialist — 1 of 15

The chosen specialist runs. There are three role groups:

### Generalist workers

| Agent | Role |
|---|---|
| [researcher](agents/workspaces/researcher/SOUL.md) | Information broker — cites every claim; produces a findings file |
| [note-maker](agents/workspaces/note-maker/SOUL.md) | Drafts vault notes from observations |
| [coder](agents/workspaces/coder/SOUL.md) | Code changes via PRs |
| [doc-writer](agents/workspaces/doc-writer/SOUL.md) | Operator-facing documentation |
| [errand-runner](agents/workspaces/errand-runner/SOUL.md) | Executes Class C+ side effects under a signed approval token (see §5) |

### Domain operators (all propose-only — class C+ writes route via errand-runner)

| Agent | Role |
|---|---|
| [homelab-engineer](agents/workspaces/homelab-engineer/SOUL.md) | Cluster + Flux + GitOps |
| [network-operator](agents/workspaces/network-operator/SOUL.md) | L1-L7 network, Omada, BGP, VLANs |
| [storage-operator](agents/workspaces/storage-operator/SOUL.md) | Ceph + Longhorn + Garage + CNPG |
| [smart-home-operator](agents/workspaces/smart-home-operator/SOUL.md) | HA + Z-Wave / Zigbee / Matter / ESPHome + Frigate + Music Assistant + voice. Loads the [device intent map](agents/workspaces/smart-home-operator/device-intent-map.yaml) on every task. |
| [ml-operator](agents/workspaces/ml-operator/SOUL.md) | Ollama lifecycle, Spark, Immich CLIP, Frigate+ |
| [observability-operator](agents/workspaces/observability-operator/SOUL.md) | Prometheus / AlertManager / Grafana / Loki / HolmesGPT |

### Vertical specialists

| Agent | Role |
|---|---|
| [property-coordinator](agents/workspaces/property-coordinator/SOUL.md) | Contractor + property workstreams |
| [health-tracker](agents/workspaces/health-tracker/SOUL.md) | Health log — **never escalates to Claude**; data stays local |

### Internal-routing agents (not normally addressed directly)

| Agent | Role |
|---|---|
| [supervisor](agents/workspaces/supervisor/SOUL.md) | Receives rejections; either reroutes or escalates to user |
| [reviewer](agents/workspaces/reviewer/SOUL.md) | Code review against PRs and drafts |
| [historian](agents/workspaces/historian/SOUL.md) | Daily-digest curator (runs on schedule; not normally routed-to) |
| [reporter](agents/workspaces/reporter/SOUL.md) | Universal final hop — see §6 |

Each specialist returns a structured output via `{"output": ...}`. The wrapper at `_with_activity_log` in [`graphs/fleet.py`](src/agents/graphs/fleet.py) records every node-invocation to the per-agent activity log at `vault/agents/<id>/memory/activity-log.md`.

---

## 4. Branching — what comes after the specialist

The conditional edge `_route_after_specialist` in [`graphs/fleet.py`](src/agents/graphs/fleet.py) picks one of three paths based on what the specialist set in `FleetState`:

| Condition | Route | Behavior |
|---|---|---|
| `state.rejection is not None` | → [supervisor](agents/workspaces/supervisor/SOUL.md) | Specialist refused the task; supervisor either re-routes to a different specialist or escalates to reporter |
| `state.approval_request is not None` AND `state.target_agent == "errand-runner"` | → [errand-runner](agents/workspaces/errand-runner/SOUL.md) | Class C+ side-effect proposal (see §5) |
| otherwise | → [reporter](agents/workspaces/reporter/SOUL.md) | Terminal — reporter renders the output |

---

## 5. Approval flow (for Class C+ side effects)

The fleet runs in **propose-then-execute** mode. Specialists never directly mutate cluster state — they produce a structured `ApprovalRequest` (signed HMAC) and route to [errand-runner](agents/workspaces/errand-runner/SOUL.md), which pauses on `interrupt()`. The pause emits a webhook to home-ops `langgraph-approval-post.ts` → Zulip DM with action buttons → ntfy phone notification.

The user verdict (approve / reject / defer) comes back via:

- The Zulip action buttons → `langgraph-approval-receive.ts` → `POST /approval` (HMAC-verified)
- Or a `@-mention` reply to the Zulip bot → `zulip-triager-webhook.ts`

`POST /approval` resumes the paused workflow with the verdict. If approved, errand-runner executes the underlying MCP call (one of the allowlisted ones in [`src/agents/tools/mcp.py`](src/agents/tools/mcp.py)). The result then routes to [reporter](agents/workspaces/reporter/SOUL.md).

If the approval doesn't return within the task's `ttl_seconds`, the awaiting-user-sweep cron fires a follow-up DM (and eventually expires the task, surfacing the loss to the user honestly via reporter).

Action classes (recorded by the wrapper in `graphs/fleet.py::_DEFAULT_ACTION_CLASS`):

- **A** — pure read / analysis. No side effects. (Most specialists default to A.)
- **B** — vault draft write (no MCP). Approval implicit (you read the draft and decide whether to publish).
- **C** — single-target MCP write via errand-runner. Signed approval required.
- **D** — multi-target / cluster-scope MCP write. Signed approval required + extra blast-radius surfacing.

---

## 6. Reporter — the universal final hop

Every chain ends at [reporter](agents/workspaces/reporter/SOUL.md). Reporter:

- Reads `state.content` (original ask), `state.output` (specialist's raw output), `state.target_agent` (who produced it), `state.rejection`, `state.approval_request`, `state.data_tier`
- Calls the Spark `qwen2.5:32b` model with reporter's persona
- Returns a **Zulip-markdown DM body** with:
  - Bold first-line conclusion
  - Clickable `obsidian://open?vault=claude&file=<path>` deep links for vault references
  - `[label](url)` markdown links for external URLs
  - Restricted-tier redaction per HOMELAB-SPEC Layer 5
  - Pass-through (minimal rewriting) when the specialist's output is already user-friendly

Reporter's rendering REPLACES `state.output` — the completion-post webhook then emits that text verbatim plus a small meta footer (agent label + duration + hai task link).

If reporter's LLM call fails, the node falls back to emitting the upstream specialist's raw output unchanged.

---

## 7. Egress — Zulip DM

When the queue worker sees the chain reach END, it fires the `completion-post` webhook to home-ops `langgraph-completion-post.ts`. The Windmill workflow:

1. Reads `state.output` (reporter's rendering)
2. Resolves the agent label from `AGENT_LABEL[target_agent]`
3. Looks up `ADMIN_NAME` from the windmill-workflows ExternalSecret (sourced from 1Password)
4. Emits the DM as a Zulip private message: reporter's body verbatim + a one-line meta footer + a `hai task show <id>` reference

The user sees the rich-text rendering on their phone (Zulip mobile renders the markdown; `obsidian://` deep-links open the Obsidian app).

---

## 8. Audit trail

Every step is observable:

- **Per-task state**: `GET /admin/tasks/<id>` returns the full checkpointer state, including the `triage` decision, every specialist's intermediate output, the activity log entries, and `accumulated_cost_usd`.
- **Per-agent activity log**: each node-invocation writes a markdown entry to `vault/agents/<id>/memory/activity-log.md` (one line per task, with task_id + action_class + outcome + summary).
- **Tracing**: every chain emits OTel spans + Langfuse traces (model + group + token count + USD per LLM call).
- **Metrics**: `/metrics` exposes per-agent counts / tokens / cost / duration; the home-ops `aihomeops-state` Grafana dashboard renders these.

---

## See also

- [`src/agents/graphs/fleet.py`](src/agents/graphs/fleet.py) — graph wiring (the conditional-edge routers + the activity-log wrapper)
- [`src/agents/state.py`](src/agents/state.py) — `FleetState` + `AgentId` + all envelope types
- [`src/agents/api/inbox.py`](src/agents/api/inbox.py) — `POST /inbox` schema + the dedup + triager-bypass logic
- [`agents/workspaces/_shared/SOUL.md`](agents/workspaces/_shared/SOUL.md) — baseline every agent inherits
- [`agents/workspaces/_shared/USER.md`](agents/workspaces/_shared/USER.md) — shared user profile (loaded for every agent)
- [`.agents/instructions/hardware-routing.md`](.agents/instructions/hardware-routing.md) — model-to-GPU routing matrix
- home-ops `docs/src/ai_architecture.md` — cluster-side component map for the AI pipeline
