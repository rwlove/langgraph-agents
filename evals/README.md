# Spark-vs-Claude per-agent eval

Answers one question per agent: **does Claude meaningfully beat qwen-on-Spark on
this agent's real work?** The verdict drives both routing paths:

- **Path 1 (escalate up):** a `route-to-api` agent gets pinned to the `claude`
  group in `agents.llm.AGENT_GROUP`.
- **Path 2 (offload down):** an `offload-safe` agent is a good target for a
  Claude-Code session to hand work to; `keep-local-fix` / `route-to-api` agents
  are not — offloading to them would either return weak output or (Path-1)
  bounce back out to the metered API.

## How it works

For each golden task the harness runs the agent's **real node** twice — once on
its default local group, once with the synthesis model forced to Claude (via
`group_override="claude"`, which fails loud if `ENABLE_CLAUDE_API` is off rather
than silently degrading). Evidence pre-fetch (`agents.nodes._evidence`) keeps
its own model on both runs, so the comparison isolates the synthesis model. An
Opus judge then scores the pair **blind** (deterministic per-task A/B ordering)
against a rubric.

`health-tracker` is Claude-**ineligible** (hard-pinned local); its Claude run is
skipped and only its local output is scored.

## Labels

| label | meaning | action |
|---|---|---|
| `offload-safe` | local is good enough on its own | safe Path-2 offload target; no escalation needed |
| `route-to-api` | eligible AND Claude clearly wins | pin to `claude` in `AGENT_GROUP` |
| `keep-local-fix` | local inadequate, escalation won't/can't rescue it | improve locally: prompt, evidence pre-fetch, model size |

Thresholds live in `report.py` (`_CLAUDE_WIN_RATE`, `_CLAUDE_DELTA`,
`_LOCAL_ADEQUATE_TOTAL`) — conservative starting points, tune as data accrues.

## Running

Needs a live cluster (Spark + MCP gateway reachable) and, for the Claude runs +
the judge, `ENABLE_CLAUDE_API=true` with `ANTHROPIC_API_KEY` set. From a laptop,
port-forward `ollama-spark` and the MCP gateway first; for fidelity, run the
full sweep as an in-cluster Job.

```bash
uv run python -m evals --agent network-operator      # one agent
uv run python -m evals --all                          # every agent with a golden set
uv run python -m evals --all --no-judge               # exercise runs only, skip judging
uv run python -m evals --agent reporter --out out.json
```

Runs are sequential — the fleet shares one Spark GPU.

## Adding a golden set

1. Create `evals/golden/<agent-id>.yaml` (`agent_id` + a `tasks` list; see
   `network-operator.yaml`). Keep tasks `public`/`internal` — `restricted`
   tasks never escalate at runtime, so a Claude comparison is meaningless.
2. Optionally add `evals/rubrics/<agent-id>.md`; otherwise `default.md` is used.
3. Seed from real `/inbox` traffic where it exists (Langfuse + the activity
   log). For agents with little history (the operators), hand-author 5–8
   representative tasks anchored to what the agent actually owns.

## Status

`network-operator` golden set is the template. Seeding the four high-traffic
agents (`reporter`, `observability-operator`, `researcher`, `homelab-engineer`)
from Langfuse is pending MCP-gateway access.
