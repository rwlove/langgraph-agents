"""`hai offload` — the Path-2 (Claude-Code -> fleet) entrypoint.

Covers the pure body-builder + agent guard + parser wiring. The HTTP/tail path
is the same code as `hai task add` (already exercised elsewhere); the only thing
unique here is that the body pins source="claude-code", which fires the router's
no-escalate guard.
"""

from __future__ import annotations

from typing import get_args

from agents.cli.main import (
    _build_parser,
    build_offload_body,
    cmd_offload,
    unknown_agent,
)
from agents.state import Source


def test_offload_body_sets_claude_code_source() -> None:
    body = build_offload_body("audit vlans", agent=None, conversation_id=None, task_id="cc-1")
    assert body["source"] == "claude-code"
    assert body["content"] == "audit vlans"
    assert body["task_id"] == "cc-1"
    assert body["user"] == "rob"
    assert "target_agent" not in body
    assert "conversation_id" not in body


def test_offload_body_pins_agent_and_conversation() -> None:
    body = build_offload_body(
        "x", agent="network-operator", conversation_id="conv-9", task_id="cc-2"
    )
    assert body["target_agent"] == "network-operator"
    assert body["conversation_id"] == "conv-9"


def test_offload_source_is_a_valid_source_literal() -> None:
    # Guards the CLI<->state<->router contract: the body's source must be a real
    # Source value, and it must be the exact string the router guard keys on.
    assert "claude-code" in get_args(Source)
    body = build_offload_body("x", agent=None, conversation_id=None, task_id="cc-3")
    assert body["source"] in get_args(Source)


def test_unknown_agent_detection() -> None:
    assert unknown_agent("not-an-agent") is True
    assert unknown_agent("network-operator") is False
    assert unknown_agent(None) is False


def test_offload_command_is_wired() -> None:
    args = _build_parser().parse_args(
        ["offload", "do a thing", "--agent", "network-operator", "--no-tail"]
    )
    assert args.func is cmd_offload
    assert args.content == "do a thing"
    assert args.agent == "network-operator"
    assert args.no_tail is True
