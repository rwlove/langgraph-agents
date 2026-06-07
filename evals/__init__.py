"""Spark-vs-Claude per-agent evaluation harness.

Answers one question per agent: *does Claude meaningfully beat qwen-on-Spark
on this agent's real work?* The verdict drives both routing paths —

- Path 1 (escalate up): a `route-to-api` agent gets pinned to the `claude`
  group in `agents.llm.AGENT_GROUP`.
- Path 2 (offload down): an `offload-safe` agent is a good target for a
  Claude-Code session to hand work to; a `keep-local-fix` / `route-to-api`
  agent is not.

The harness runs each agent's *real* node twice — once on its default local
group, once forced to Claude — over a hand-curated golden task set, then has
an Opus judge score the pair blind. It is dev tooling: it lives at the repo
root (not inside the shipped `agents` package) and needs a live cluster
(Spark + MCP gateway) plus `ENABLE_CLAUDE_API=true` to run.

See ``evals/README.md`` for the run procedure.
"""

from __future__ import annotations
