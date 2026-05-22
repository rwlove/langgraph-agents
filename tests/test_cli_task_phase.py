"""Unit tests for the CLI's phase-collapsing logic.

The queue lifecycle (pending/claimed/done/failed/dlq) and the graph
lifecycle (interrupts, awaiting_user_since) are orthogonal in the
backend, but the operator wants ONE label per task. `_task_phase`
collapses them; these tests pin the precedence.
"""

from __future__ import annotations

from agents.cli.main import (
    _PHASE_AWAITING,
    _PHASE_COMPLETE,
    _PHASE_FAILED,
    _PHASE_PENDING,
    _PHASE_RUNNING,
    _PHASE_UNKNOWN,
    _task_phase,
    _truncate,
)


def test_interrupt_present_means_awaiting_even_if_queue_says_done() -> None:
    """Graph paused at interrupt() while the worker registers as done.

    From the user's view that's `awaiting`, not `complete`. Without
    this precedence, the table would lie about which tasks need
    attention.
    """
    t = {"queue_status": "done", "interrupts": [{"id": "x"}], "awaiting_user_since": None}
    assert _task_phase(t) == _PHASE_AWAITING


def test_awaiting_user_since_alone_is_awaiting() -> None:
    t = {"queue_status": "done", "interrupts": [], "awaiting_user_since": "2026-05-21T20:00:00Z"}
    assert _task_phase(t) == _PHASE_AWAITING


def test_queue_done_no_interrupt_is_complete() -> None:
    t = {"queue_status": "done", "interrupts": [], "awaiting_user_since": None}
    assert _task_phase(t) == _PHASE_COMPLETE


def test_queue_pending_is_pending() -> None:
    t = {"queue_status": "pending", "interrupts": [], "awaiting_user_since": None}
    assert _task_phase(t) == _PHASE_PENDING


def test_queue_claimed_is_running() -> None:
    t = {"queue_status": "claimed", "interrupts": [], "awaiting_user_since": None}
    assert _task_phase(t) == _PHASE_RUNNING


def test_failed_and_dlq_both_collapse_to_failed() -> None:
    assert _task_phase({"queue_status": "failed", "interrupts": []}) == _PHASE_FAILED
    assert _task_phase({"queue_status": "dlq", "interrupts": []}) == _PHASE_FAILED


def test_missing_queue_status_yields_unknown() -> None:
    t: dict[str, object] = {"interrupts": [], "awaiting_user_since": None}
    assert _task_phase(t) == _PHASE_UNKNOWN


def test_truncate_first_line_only() -> None:
    """Content can be multi-line; the table cell needs one line."""
    assert _truncate("line1\nline2\nline3", 70) == "line1"


def test_truncate_long_string_gets_ellipsis() -> None:
    assert _truncate("a" * 100, 10) == "a" * 9 + "…"


def test_truncate_empty_string() -> None:
    assert _truncate("", 10) == ""
    assert _truncate(None, 10) == ""  # type: ignore[arg-type]
