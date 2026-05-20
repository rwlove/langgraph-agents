# Integration scripts

Operator-run, not pytest-collected (with one exception below). These exist
to repro tricky failure modes against a real Postgres so we stop iterating
in production.

## `repro_checkpoint_hang.py` — reporter post-node checkpoint hang

### What it reproduces

Home-ops memory entry `project_langgraph_reporter_post_node_hang`,
reconfirmed against v0.2.23 on 2026-05-20:

> `POST /inbox` → `inbox_start` → `node_start agent=triager` → no
> `node_end` after 120s. `/admin/asyncio-tasks` showed multiple
> `AsyncPostgresSaver.aput_writes` and
> `_checkpointer_put_after_previous → aput → __aenter__` tasks all
> blocked at `contextlib.__aenter__` waiting on a connection from the
> pool. Postgres-side: only 1 active connection in
> `idle ClientRead` state. Pool was effectively starved despite no
> obvious in-flight queries.

Suspected cause is the `langgraph-checkpoint-postgres` 3.1.0 ↔
`psycopg-pool` interaction inside `AsyncPostgresSaver.aput_writes` —
something in the path acquires a pool slot via `__aenter__` and parks
without releasing.

### Why a script, not a pytest test

A unit-test asserting "graph completes in 10s" misses the
intermittent, state-dependent failure mode. The bug needs:

- a real Postgres on the other end (the pool's silent-half-open paths
  only manifest against a real TCP socket),
- the production pool kwargs verbatim (`tcp_user_timeout=15000`,
  `keepalives*`, `max_idle=60`, `check=check_connection`),
- concurrent inbox traffic so multiple `aput_writes` coroutines race
  for pool slots.

When the parking happens it parks *forever* (or until the FastAPI
request times out), so the harness samples `/admin/asyncio-tasks` +
`pg_stat_activity` while requests are in flight and prints whatever
parking signature it sees.

### Methodology

The harness fires N concurrent `POST /inbox` requests against the
real FastAPI app (in-process via `httpx.ASGITransport`, no socket
bound) and runs a background sampler every 2s. Each sample captures:

| Field                            | Meaning                                                                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `asyncio_tasks_total`            | Count of asyncio Tasks in the loop. Baseline ~3–5; spikes track in-flight requests.                                                                    |
| `checkpoint_blocked_at_aenter`   | Count of Tasks whose coro repr matches the checkpointer (`aput_writes` / `_checkpointer_put_after_previous` / `aput`) AND innermost await is `__aenter__`. **Nonzero = the production hang signature.** |
| `pg_activity.by_state`           | postgres-side: how many backends, in what state. `idle ClientRead` count vs pool max_size flags pool starvation.                                       |
| `pg_activity.waiting`            | Count of backends with a `wait_event_type` set. Should be 0 in steady state.                                                                           |
| `blocked_coros`                  | First 5 coro reprs that matched the hang signature this tick. Useful artifact when filing a follow-up bug.                                             |

LLM calls are NOT exercised — the triager + reporter NODES are
patched with cheap synchronous returns so the only Postgres traffic
is checkpoint writes. This isolates the checkpoint layer from any
Ollama / Claude latency.

### State space

Lever to vary when chasing the bug:

| Lever                            | What it changes                                                                                                                          |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `--concurrent-requests N`        | The most direct trigger. Default 5. The pool's `max_size=10`; sweep 5 → 15 to find the starvation threshold.                             |
| `--observation-window-seconds T` | Wall-clock budget. Default 120s (matches the production timeout that surfaced the hang).                                                 |
| `--request-timeout-seconds T`    | Per-request timeout. Default 60s. Lowering this surfaces "requests would have completed but ran past their budget" vs true hangs.        |
| `--sample-interval-seconds T`    | Sampler tick. Default 2s. Tighten for higher temporal resolution; loosen if the sampler itself is contending for the pool.               |

Each run also tries `POSTGRES_LOG_STATEMENT=all` (compose-time env
var) for postgres-side statement logs. Use sparingly — the postgres
log fills fast at concurrent-write rates.

### How to run

```bash
# 1. Bring up postgres (port 55432, offset from any laptop-local 5432).
docker compose -f tests/integration/docker-compose.yml up -d

# 2. Run the harness from the repo root. Default flags reproduce the
#    documented production conditions.
uv run python tests/integration/repro_checkpoint_hang.py

# 3. Sweep concurrency to find the starvation threshold.
for n in 3 5 7 10 12 15; do
  uv run python tests/integration/repro_checkpoint_hang.py \
    --concurrent-requests "$n" \
    --artifact "repro-c${n}.json"
done

# 4. Optional: postgres-side statement log for one run.
POSTGRES_LOG_STATEMENT=all docker compose \
  -f tests/integration/docker-compose.yml up -d
# ... rerun the harness, then:
docker logs langgraph-checkpoint-repro-pg | grep -i 'BEGIN\|COMMIT\|INSERT.*checkpoint'

# 5. Tear down when done.
docker compose -f tests/integration/docker-compose.yml down -v
```

### Exit codes

- `0` — harness ran cleanly, no parked-at-`__aenter__` tasks observed.
- `1` — at least one sample tick saw a Task parked in the
  checkpointer path at `__aenter__` (the hang signature). The
  artifact JSON contains the offending coro reprs.

CI does not run this script — it's operator-paced and the failure
condition is "we still don't understand it." Treat exit-1 as a data
point to attach to a follow-up issue, not a build break.

### Artifact format

Each run writes a JSON file (default `repro_checkpoint_hang.json`)
with the full sample series plus per-request status. Drop it onto a
GitHub issue or share via the home-ops obsidian vault. Example
top-level shape:

```json
{
  "config": { "concurrent_requests": 5, "observation_window_seconds": 120, ... },
  "results": [
    { "task_id": "repro-abc123", "status": "complete",            "elapsed_s": 0.42, "error": null },
    { "task_id": "repro-def456", "status": "observation_timeout", "elapsed_s": 120.0, "error": "window closed before request returned" }
  ],
  "samples": [
    { "t_offset_s": 2.01, "asyncio_tasks_total": 12, "checkpoint_blocked_at_aenter": 4,
      "pg_activity": { "by_state": { "idle": 1, "active": 0 }, "waiting": 0 },
      "blocked_coros": [ "<coroutine object AsyncPostgresSaver.aput_writes at ...>", ... ] }
  ]
}
```

### Followups (DO NOT fix in the same PR as this harness)

If a run reproduces the hang, capture the artifact and file a new PR
that tries one fix at a time. Candidates worth bisecting:

1. Bump `langgraph-checkpoint-postgres` (3.1.0 is the version
   implicated; check the project's changelog for `aput_writes`
   pool-handling fixes).
2. Swap `max_size=10` for `max_size=20`. The pool starvation
   signature SHOULD go away if it's pure exhaustion — if it
   doesn't, that rules pure-exhaustion out and points at a leak.
3. Replace `AsyncConnectionPool.check_connection` with a no-op
   `check=` to test whether the check itself is the parking
   point.
4. Add per-pool-acquire timing in a forked `AsyncPostgresSaver` to
   localize whether the park is at-acquire or post-acquire.

## `test_checkpoint_pool.py` — pytest regression

This one IS a pytest test — runs under `pytest tests/integration/`
when `POSTGRES_TEST_URL` is set. Covers the v0.2.11 single-conn hang
that the `AsyncConnectionPool` migration fixed. Different bug, same
neighborhood — keep both around.
