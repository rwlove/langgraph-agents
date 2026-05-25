"""`hai` CLI entry point.

Subcommands:

    hai task add "<content>" [--no-tail]
    hai task tail <task_id>
    hai task ls
    hai task show <task_id>

    hai todo add "<body>" [--tag T]*
    hai todo ls [--all]
    hai todo done <id>

    hai cost [--days N] [--json]
    hai auth set-token <value>
    hai auth show

All commands read config + token from `~/.config/hai/`. See
`agents.cli.config` for the layout.

The CLI is a thin HTTP client over langgraph-agents — see the
home-ops Stage 2 gap analysis for the contract.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

from agents.cli.client import HaiClient
from agents.cli.config import (
    CONFIG_FILE,
    TOKEN_FILE,
    Config,
    load_config,
    write_default_config,
    write_token,
)

# ---------- Helpers ----------


def _ulid_ish_id(prefix: str) -> str:
    """Client-side task_id hint. Server assigns the real ULID."""
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _need_token(cfg: Config) -> None:
    if cfg.token is None:
        print(
            "error: no auth token configured.\n"
            "Run: hai auth set-token <value>\n"
            "Get the value from 1Password → Kubernetes vault → hai-cli → TOKEN.",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------- task subcommands ----------


def cmd_task_add(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        body: dict[str, Any] = {
            "task_id": _ulid_ish_id("cli"),
            "source": "cli",
            "user": "rob",
            "content": args.content,
        }
        if args.conversation_id:
            body["conversation_id"] = args.conversation_id
        resp = client.request("POST", "/inbox", json=body)
        assert isinstance(resp, dict)
        task_id = resp["task_id"]
        conversation_id = resp.get("conversation_id", task_id)
        print(
            f"task_id: {task_id}  conversation_id: {conversation_id}"
            f"  status: {resp.get('status', 'accepted')}"
        )
        if args.no_tail:
            return
        _tail(client, cfg, task_id)


def cmd_task_tail(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        _tail(client, cfg, args.task_id)


def _tail(client: HaiClient, cfg: Config, task_id: str) -> None:
    """Poll /admin/tasks/<id> until terminal, print the output."""
    deadline = time.monotonic() + cfg.poll_timeout_s
    last_status: str | None = None
    while time.monotonic() < deadline:
        resp = client.request("GET", f"/admin/tasks/{task_id}")
        assert isinstance(resp, dict)
        queue = resp.get("queue") or {}
        status = queue.get("status")
        if status != last_status:
            print(f"  [{time.strftime('%H:%M:%S')}] status={status or '?'}")
            last_status = status
        if status in ("done", "failed", "dlq"):
            output = (queue.get("result") or {}).get("output")
            if output:
                print()
                print(output)
            else:
                err = queue.get("last_error")
                if err:
                    print(f"error: {err}", file=sys.stderr)
            return
        time.sleep(cfg.poll_interval_s)
    print(
        f"timed out waiting for {task_id} (limit {cfg.poll_timeout_s}s). "
        f"Re-tail with: hai task tail {task_id}",
        file=sys.stderr,
    )
    sys.exit(1)


_PHASE_RUNNING = "running"
_PHASE_AWAITING = "awaiting"
_PHASE_COMPLETE = "complete"
_PHASE_FAILED = "failed"
_PHASE_PENDING = "pending"
_PHASE_UNKNOWN = "?"


def _task_phase(task: dict[str, Any]) -> str:
    """Collapse queue_status + interrupts + awaiting_user_since into one label.

    The queue lifecycle and the graph lifecycle are orthogonal: a task
    can be queue-status=done while the graph is paused at an
    `interrupt()` waiting for the user. From the user's view, that's
    `awaiting`, not `complete` — and that's the bug the table format
    is here to fix.
    """
    interrupts = task.get("interrupts") or []
    awaiting_since = task.get("awaiting_user_since")
    if interrupts or awaiting_since:
        return _PHASE_AWAITING
    qs = task.get("queue_status") or ""
    if qs == "pending":
        return _PHASE_PENDING
    if qs == "claimed":
        return _PHASE_RUNNING
    if qs == "done":
        return _PHASE_COMPLETE
    if qs in ("failed", "dlq"):
        return _PHASE_FAILED
    return _PHASE_UNKNOWN


def _truncate(s: str, n: int) -> str:
    s = (s or "").splitlines()[0] if s else ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_task_table(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        print("(no tasks)")
        return
    print(f"{'TASK_ID':30s}  {'PHASE':9s}  {'AGENT':22s}  CONTENT")
    for t in tasks:
        tid = (t.get("task_id") or "?")[:30]
        phase = _task_phase(t)
        agent = (t.get("target_agent") or "—")[:22]
        content = _truncate(t.get("content") or "", 70)
        print(f"{tid:30s}  {phase:9s}  {agent:22s}  {content}")


def _fetch_tasks(client: HaiClient) -> list[dict[str, Any]]:
    resp = client.request("GET", "/admin/tasks")
    assert isinstance(resp, list)
    return resp


def cmd_task_ls(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        tasks = _fetch_tasks(client)
    if args.json:
        _print_json(tasks)
        return
    _render_task_table(tasks)


def cmd_awaiting(args: argparse.Namespace) -> None:
    """Show only the tasks needing the user's input — the highest-signal view."""
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        tasks = [t for t in _fetch_tasks(client) if _task_phase(t) == _PHASE_AWAITING]
    if args.json:
        _print_json(tasks)
        return
    _render_task_table(tasks)


def cmd_completed(args: argparse.Namespace) -> None:
    """Show tasks the agents finished (queue done, no pending interrupt)."""
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        tasks = [t for t in _fetch_tasks(client) if _task_phase(t) == _PHASE_COMPLETE]
    tasks = tasks[: args.limit]
    if args.json:
        _print_json(tasks)
        return
    _render_task_table(tasks)


def cmd_task_show(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        resp = client.request("GET", f"/admin/tasks/{args.task_id}")
        _print_json(resp)


# ---------- todo subcommands ----------


def cmd_todo_add(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        body: dict[str, Any] = {"body": args.body, "tags": args.tag or []}
        resp = client.request("POST", "/admin/todos", json=body)
        _print_json(resp)


def cmd_todo_ls(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        params = {"status": "all" if args.all else "open"}
        resp = client.request("GET", "/admin/todos", params=params)
        if not isinstance(resp, list):
            _print_json(resp)
            return
        if not resp:
            print("(no todos)")
            return
        for todo in resp:
            tags = ", ".join(todo.get("tags") or [])
            tag_str = f" [{tags}]" if tags else ""
            print(f"  {todo['id']}  {todo['status']:7s}  {todo['body']}{tag_str}")


def cmd_todo_done(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        resp = client.request("PATCH", f"/admin/todos/{args.todo_id}", json={"status": "done"})
        _print_json(resp)


# ---------- cost ----------


def _render_cost_table(data: dict[str, Any]) -> None:
    """Pretty-print usage stats from /admin/cost."""
    days = data.get("days", "?")
    total = data.get("total_tasks", 0)
    print(f"Task completions — last {days} day(s)   total: {total}")
    print()

    by_agent: dict[str, int] = data.get("by_agent") or {}
    if by_agent:
        print(f"{'AGENT':30s}  {'COUNT':>6s}")
        print(f"{'─' * 30}  {'─' * 6}")
        for agent, count in by_agent.items():
            print(f"{agent:30s}  {count:>6d}")
        print()

    by_source: dict[str, int] = data.get("by_source") or {}
    if by_source:
        print(f"{'SOURCE':30s}  {'COUNT':>6s}")
        print(f"{'─' * 30}  {'─' * 6}")
        for source, count in by_source.items():
            print(f"{source:30s}  {count:>6d}")

    print()
    print(
        "# TODO: add model/local vs claude column when provenance lands"
    )


def cmd_cost(args: argparse.Namespace) -> None:
    cfg = load_config()
    _need_token(cfg)
    with HaiClient(cfg) as client:
        resp = client.request("GET", "/admin/cost", params={"days": args.days})
    if args.json:
        _print_json(resp)
        return
    assert isinstance(resp, dict)
    _render_cost_table(resp)


# ---------- auth ----------


def cmd_auth_set_token(args: argparse.Namespace) -> None:
    write_token(args.value)
    cfg = load_config()
    if cfg.token is None:
        print("error: token written but reload failed", file=sys.stderr)
        sys.exit(1)
    print(f"token written to {TOKEN_FILE}  (length={len(cfg.token)})")


def cmd_auth_show(_args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg.token is None:
        masked = "(unset)"
    else:
        masked = f"{cfg.token[:6]}…{cfg.token[-4:]} (len={len(cfg.token)})"
    print(f"endpoint:        {cfg.endpoint}")
    print(f"token:           {masked}")
    print(f"config file:     {CONFIG_FILE}{' (exists)' if CONFIG_FILE.exists() else ' (missing)'}")
    print(f"token file:      {TOKEN_FILE}{' (exists)' if TOKEN_FILE.exists() else ' (missing)'}")
    print(f"poll_interval_s: {cfg.poll_interval_s}")
    print(f"poll_timeout_s:  {cfg.poll_timeout_s}")


def cmd_auth_init(args: argparse.Namespace) -> None:
    write_default_config(args.endpoint)
    print(f"config written to {CONFIG_FILE}")
    print("now run: hai auth set-token <value>")


# ---------- argparse wiring ----------


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    p = argparse.ArgumentParser(
        prog="hai",
        description="Rob's CLI to the HomeAIOps pipeline.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # task
    p_task = sub.add_parser("task", help="task ingress + tracking")
    s_task = p_task.add_subparsers(dest="task_cmd", required=True)

    p_task_add = s_task.add_parser("add", help="POST a task and tail until done")
    p_task_add.add_argument("content")
    p_task_add.add_argument(
        "--no-tail", action="store_true", help="don't block waiting for the result"
    )
    p_task_add.add_argument(
        "--conversation-id",
        metavar="ID",
        default=None,
        help="continue an existing conversation thread (pass conversation_id from prior response)",
    )
    p_task_add.set_defaults(func=cmd_task_add)

    p_task_tail = s_task.add_parser("tail", help="poll an existing task until terminal")
    p_task_tail.add_argument("task_id")
    p_task_tail.set_defaults(func=cmd_task_tail)

    p_task_ls = s_task.add_parser("ls", help="list recent tasks (table format)")
    p_task_ls.add_argument(
        "--json", action="store_true", help="emit raw JSON instead of the table"
    )
    p_task_ls.set_defaults(func=cmd_task_ls)

    p_task_show = s_task.add_parser("show", help="show one task's full state")
    p_task_show.add_argument("task_id")
    p_task_show.set_defaults(func=cmd_task_show)

    # awaiting — tasks paused for the user's input
    p_awaiting = sub.add_parser(
        "awaiting", help="show only tasks needing user input (interrupts)"
    )
    p_awaiting.add_argument("--json", action="store_true", help="emit raw JSON")
    p_awaiting.set_defaults(func=cmd_awaiting)

    # completed — what the agents finished
    p_completed = sub.add_parser(
        "completed", help="show only completed tasks (queue done, no pending interrupt)"
    )
    p_completed.add_argument(
        "--limit", type=int, default=20, help="max rows to show (default 20)"
    )
    p_completed.add_argument("--json", action="store_true", help="emit raw JSON")
    p_completed.set_defaults(func=cmd_completed)

    # todo
    p_todo = sub.add_parser("todo", help="durable todo store")
    s_todo = p_todo.add_subparsers(dest="todo_cmd", required=True)

    p_todo_add = s_todo.add_parser("add", help="create a todo")
    p_todo_add.add_argument("body")
    p_todo_add.add_argument("--tag", action="append", help="tag (repeatable)")
    p_todo_add.set_defaults(func=cmd_todo_add)

    p_todo_ls = s_todo.add_parser("ls", help="list open todos (use --all for everything)")
    p_todo_ls.add_argument("--all", action="store_true")
    p_todo_ls.set_defaults(func=cmd_todo_ls)

    p_todo_done = s_todo.add_parser("done", help="mark a todo done")
    p_todo_done.add_argument("todo_id")
    p_todo_done.set_defaults(func=cmd_todo_done)

    # cost
    p_cost = sub.add_parser("cost", help="show task usage counts by agent and source")
    p_cost.add_argument(
        "--days",
        type=int,
        default=7,
        metavar="N",
        help="look-back window in days (default 7)",
    )
    p_cost.add_argument("--json", action="store_true", help="emit raw JSON instead of the table")
    p_cost.set_defaults(func=cmd_cost)

    # auth
    p_auth = sub.add_parser("auth", help="manage CLI auth")
    s_auth = p_auth.add_subparsers(dest="auth_cmd", required=True)

    p_auth_init = s_auth.add_parser("init", help="write a baseline config.toml")
    p_auth_init.add_argument("endpoint", help="e.g. https://hai.example.com")
    p_auth_init.set_defaults(func=cmd_auth_init)

    p_auth_set = s_auth.add_parser(
        "set-token", help="store the Bearer token at ~/.config/hai/token"
    )
    p_auth_set.add_argument("value")
    p_auth_set.set_defaults(func=cmd_auth_set_token)

    p_auth_show = s_auth.add_parser("show", help="show current config (token masked)")
    p_auth_show.set_defaults(func=cmd_auth_show)

    return p


def main() -> None:
    args = _build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
