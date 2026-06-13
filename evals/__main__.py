"""CLI: run the Spark-vs-Claude eval over one or more agents.

    uv run python -m evals --agent network-operator
    uv run python -m evals --all
    uv run python -m evals --all --no-judge            # runs only, skip judging
    uv run python -m evals --agent reporter --out results.json

Requires a live cluster (Spark + MCP gateway) and, for the Claude runs + the
judge, ``ENABLE_CLAUDE_API=true`` with ``ANTHROPIC_API_KEY`` set. Runs are
sequential on purpose — the fleet shares one Spark GPU, so parallel sweeps would
just contend.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import cast

from agents.state import AgentId
from evals import runner
from evals.judge import errored_verdict, judge_pair, score_single
from evals.loader import available_agents, load_golden_for, load_rubric
from evals.registry import is_claude_eligible
from evals.report import build_report
from evals.runner import run_task
from evals.schema import AgentReport, GoldenTask, JudgeVerdict


async def _verdict_for(
    agent_id: AgentId, task: GoldenTask, rubric: str, *, judge: bool
) -> JudgeVerdict:
    local = await run_task(agent_id, task, "local")
    claude = await run_task(agent_id, task, "claude")

    if not judge:
        # Caller just wants runs exercised; emit a placeholder errored verdict
        # so the report counts it as unjudged rather than scoring it.
        note = f"local_error={local.error}" if local.error else "no-judge mode"
        return errored_verdict(agent_id, task.task_id, note)

    if local.error:
        return errored_verdict(agent_id, task.task_id, f"local run failed: {local.error}")

    # Pair only when there's a genuine Claude output to compare against.
    if is_claude_eligible(agent_id) and not claude.skipped and not claude.error:
        return await judge_pair(agent_id, task, local, claude, rubric)
    return await score_single(agent_id, task, local, rubric)


async def _run_agent(agent_id: AgentId, *, judge: bool) -> AgentReport:
    golden = load_golden_for(agent_id)
    rubric = load_rubric(agent_id)
    verdicts: list[JudgeVerdict] = []
    for task in golden.tasks:
        verdicts.append(await _verdict_for(agent_id, task, rubric, judge=judge))
    return build_report(agent_id, verdicts)


# (short label, dimension key) — order matches rubrics/default.md.
_DIM_COLS = (
    ("corr", "correctness"),
    ("compl", "completeness"),
    ("safety", "safety_gate"),
    ("action", "actionability"),
)


def _dim_line(r: AgentReport) -> str:
    """Per-dimension `local->claude` (1..5) sub-line — the acceptability read."""
    parts = []
    for short, key in _DIM_COLS:
        local = r.mean_local_dims.get(key)
        claude = r.mean_claude_dims.get(key)
        if local is None:
            parts.append(f"{short} -")
        elif claude is None:
            parts.append(f"{short} {local:.1f}")
        else:
            parts.append(f"{short} {local:.1f}->{claude:.1f}")
    return "    dims  " + "  ".join(parts)


def _print_table(reports: list[AgentReport]) -> None:
    header = f"{'agent':<22} {'label':<14} {'elig':<4} {'n':>3} {'win%':>5} {'Δ':>5} {'loc/20':>7}"
    print(header)
    print("-" * len(header))
    for r in reports:
        print(
            f"{r.agent_id:<22} {r.label:<14} {('yes' if r.eligible else 'no'):<4} "
            f"{r.n_judged:>3} {r.claude_win_rate * 100:>4.0f}% "
            f"{r.mean_score_delta:>+5.1f} {r.mean_local_total:>7.1f}"
        )
        print(_dim_line(r))
    print(
        "\nread: Δ = mean(claude-local) gap /20; loc/20 = local's own total. "
        "dims are local->claude /5.\n"
        "      corr + safety are the dealbreakers — local <4 there is not "
        "acceptable for an ops agent,\n"
        "      regardless of Δ. compl + action gaps are softer (thinner output, "
        "usually tolerable)."
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evals", description="Spark-vs-Claude per-agent eval")
    parser.add_argument(
        "--agent", action="append", default=[], help="agent id (repeatable); else use --all"
    )
    parser.add_argument("--all", action="store_true", help="run every agent with a golden set")
    parser.add_argument("--no-judge", action="store_true", help="run nodes only, skip judging")
    parser.add_argument("--out", default=None, help="write full reports as JSON to this path")
    parser.add_argument(
        "--timeout",
        type=float,
        default=runner.NODE_TIMEOUT_S,
        help=f"per-node wall-clock cap in seconds (default {runner.NODE_TIMEOUT_S:.0f}); a node "
        "exceeding it is recorded as an error instead of stalling the sweep",
    )
    return parser.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    on_disk = available_agents()
    selected = on_disk if args.all else list(args.agent)
    if not selected:
        print("nothing to run: pass --agent <id> (repeatable) or --all", file=sys.stderr)
        print(f"golden sets on disk: {', '.join(on_disk) or '(none)'}", file=sys.stderr)
        return 2

    unknown = [a for a in selected if a not in on_disk]
    if unknown:
        print(f"no golden set for: {', '.join(unknown)}", file=sys.stderr)
        return 2

    runner.NODE_TIMEOUT_S = args.timeout
    print(
        f"per-node timeout {args.timeout:.0f}s; each run's vault draft is captured + "
        "removed (no real-vault pollution)",
        file=sys.stderr,
    )

    def _persist(reps: list[AgentReport]) -> None:
        if not args.out:
            return
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump([r.model_dump() for r in reps], fh, indent=2)

    reports: list[AgentReport] = []
    for agent_id in selected:
        print(f"running {agent_id} ...", file=sys.stderr)
        reports.append(await _run_agent(cast("AgentId", agent_id), judge=not args.no_judge))
        _persist(reports)  # incremental: a mid-sweep crash keeps completed agents

    _print_table(reports)
    if args.out:
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
