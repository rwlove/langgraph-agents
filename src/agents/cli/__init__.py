"""`hai` CLI — Rob's primary interface to the HomeAIOps pipeline.

The CLI is a thin client over the langgraph-agents HTTP API. It plugs
into the same `/inbox` contract every other surface uses (HA voice,
Zulip DM, Open WebUI, AlertManager, ntfy). All shared concerns — task
schema, queue, dispatch, result handling — live in the pipeline; this
CLI just speaks to it.

See `docs/src/stage2_gap_analysis.md` in home-ops for the design.

Console-script entry point: `hai` → `agents.cli.main:main`.
"""
