"""Tests for Claude cost caps (P3.1 + the per-task / per-agent-daily follow-up).

Scope (mirrors the audit + the implementation):

- ``observability.global_claude_spend_usd()`` sums the in-process
  ``langgraph_cost_usd_total`` Counter across every label combination
  where ``group="claude"`` (and ignores local-* spend, which is
  always $0 anyway but should be excluded by the filter).
- ``llm._build_claude`` raises ``CostCapHit`` when ANY of the three
  caps is at or above its threshold:
  - ``global_daily`` — global Counter sum vs ``cost_cap_global_daily_usd``.
  - ``per_agent_daily`` — ``agent_daily_spend_usd(agent)`` vs
    ``cost_cap_per_agent_daily_usd``.
  - ``per_task`` — ``task_spend_usd(task_id)`` vs
    ``cost_cap_per_task_usd``, but ONLY if a ``task_id`` is bound on
    structlog contextvars at the call site.
- Below-threshold + no spend yet → returns a ``ChatAnthropic`` as
  usual (regression guard so the cap doesn't accidentally block
  every Claude call).
- task_id missing from contextvars → per-task check is skipped silently,
  global + per-agent still apply.
- ``_evict_old_agent_daily`` drops entries older than 7 days.

The fixtures reset the Counter via ``_value.set(0)`` per-child rather
than re-registering, because re-creating a Counter with the same name
collides in the default Prom registry. They also clear the per-task +
per-agent-daily dicts via ``_reset_cost_accumulators``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import structlog
from langchain_anthropic import ChatAnthropic

from agents.llm import CostCapHit, llm
from agents.observability import (
    _agent_daily_spend,
    _evict_old_agent_daily,
    _reset_cost_accumulators,
    agent_daily_spend_usd,
    global_claude_spend_usd,
    langgraph_cost_usd_total,
    record_task_spend,
    task_spend_usd,
)
from agents.settings import get_settings


def _reset_cost_counter() -> None:
    """Zero out every child of ``langgraph_cost_usd_total``.

    The Counter is a process-global singleton in the default Prom
    registry — we can't tear it down and recreate, but we CAN reset
    each labeled child's internal value to 0. That's enough to make
    tests independent of each other and of any earlier llm() calls.
    """
    for child in langgraph_cost_usd_total._metrics.values():
        child._value.set(0)


@pytest.fixture(autouse=True)
def _reset_clients() -> None:
    """Clear cached settings + zero the cost Counter + dicts between tests."""
    get_settings.cache_clear()
    _reset_cost_counter()
    _reset_cost_accumulators()
    structlog.contextvars.clear_contextvars()
    yield
    get_settings.cache_clear()
    _reset_cost_counter()
    _reset_cost_accumulators()
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# global_claude_spend_usd helper
# ---------------------------------------------------------------------------


def test_global_claude_spend_usd_sums_claude_group_only() -> None:
    """Helper sums only ``group="claude"`` samples; local-* is excluded."""
    langgraph_cost_usd_total.labels(agent="coder", group="claude", model="claude-opus-4-7").inc(2.5)
    langgraph_cost_usd_total.labels(agent="historian", group="claude", model="claude-opus-4-7").inc(
        1.25
    )
    # Local groups don't usually incur cost, but if a downstream emitter
    # ever stamped some, the helper must filter them out.
    langgraph_cost_usd_total.labels(agent="triager", group="local-p40", model="qwen2.5:7b").inc(
        99.0
    )

    assert global_claude_spend_usd() == pytest.approx(3.75)


def test_global_claude_spend_usd_zero_when_no_calls() -> None:
    """Fresh process → no Counter children incremented → 0.0."""
    assert global_claude_spend_usd() == 0.0


# ---------------------------------------------------------------------------
# Global daily cap (regression guard — must keep working post per-task wiring)
# ---------------------------------------------------------------------------


def test_build_claude_raises_global_daily_at_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global-daily cap reached → ``CostCapHit(cap_kind='global_daily')``.

    Uses escalate=True so the factory routes to ``_build_claude``
    regardless of local-Ollama health. The cap is set to 1.0 USD and
    the counter is bumped to exactly 1.0 to verify ``>=`` semantics
    (at-threshold is over-budget, not under).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1.0")

    langgraph_cost_usd_total.labels(agent="coder", group="claude", model="claude-opus-4-7").inc(1.0)

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(CostCapHit) as excinfo,
    ):
        llm("coder", escalate=True)

    assert excinfo.value.cap_kind == "global_daily"
    assert excinfo.value.cap_usd == pytest.approx(1.0)
    assert excinfo.value.accumulated_usd == pytest.approx(1.0)
    assert excinfo.value.agent_id == "coder"


def test_build_claude_raises_global_daily_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strictly-above-cap also raises (regression guard for ``>`` vs ``>=``)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "5.0")

    langgraph_cost_usd_total.labels(agent="historian", group="claude", model="claude-opus-4-7").inc(
        7.5
    )

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(CostCapHit) as excinfo,
    ):
        llm("coder", escalate=True)

    assert excinfo.value.cap_kind == "global_daily"
    assert excinfo.value.accumulated_usd == pytest.approx(7.5)


def test_build_claude_returns_client_when_below_all_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three caps under threshold → factory returns ``ChatAnthropic``.

    Regression guard: the cap check must not block every Claude call,
    only ones at-or-above the cap.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "10.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "10.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "5.0")

    langgraph_cost_usd_total.labels(agent="coder", group="claude", model="claude-opus-4-7").inc(2.5)

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)

    assert isinstance(model, ChatAnthropic)


# ---------------------------------------------------------------------------
# Per-agent-daily cap
# ---------------------------------------------------------------------------


def test_per_agent_daily_cap_fires_when_agent_sum_exceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_task_spend`` for ``coder`` exceeds per-agent cap → fires."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Set global high enough that only per-agent could trip.
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "2.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "1000.0")

    record_task_spend(task_id="task-A", agent="coder", cost_usd=2.0)

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(CostCapHit) as excinfo,
    ):
        llm("coder", escalate=True)

    assert excinfo.value.cap_kind == "per_agent_daily"
    assert excinfo.value.cap_usd == pytest.approx(2.0)
    assert excinfo.value.accumulated_usd == pytest.approx(2.0)


def test_per_agent_daily_cap_isolates_per_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reporter``'s spend doesn't trip ``coder``'s cap (and vice versa)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "2.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "1000.0")

    # Reporter blew its budget but coder hasn't spent anything yet.
    record_task_spend(task_id="task-A", agent="historian", cost_usd=5.0)

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)

    assert isinstance(model, ChatAnthropic)
    # And the per-agent getter reflects only `reporter`'s spend, not coder's.
    assert agent_daily_spend_usd("historian") == pytest.approx(5.0)
    assert agent_daily_spend_usd("coder") == 0.0


# ---------------------------------------------------------------------------
# Per-task cap
# ---------------------------------------------------------------------------


def test_per_task_cap_fires_when_task_sum_exceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task_id bound on contextvars + cumulative task spend ≥ cap → fires."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Set global + per-agent high enough that only per-task could trip.
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "0.50")

    structlog.contextvars.bind_contextvars(task_id="task-XYZ")
    record_task_spend(task_id="task-XYZ", agent="coder", cost_usd=0.50)

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(CostCapHit) as excinfo,
    ):
        llm("coder", escalate=True)

    assert excinfo.value.cap_kind == "per_task"
    assert excinfo.value.cap_usd == pytest.approx(0.50)
    assert excinfo.value.accumulated_usd == pytest.approx(0.50)


def test_per_task_cap_isolates_per_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different task_id starts at 0 and isn't tripped by another's spend."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "0.50")

    # task-A blew its budget; task-B is fresh.
    record_task_spend(task_id="task-A", agent="coder", cost_usd=0.50)
    structlog.contextvars.bind_contextvars(task_id="task-B")

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)

    assert isinstance(model, ChatAnthropic)
    assert task_spend_usd("task-A") == pytest.approx(0.50)
    assert task_spend_usd("task-B") == 0.0


def test_per_task_cap_skipped_when_task_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task_id missing from contextvars → per-task check skipped silently.

    Scheduled-job paths that never bind a task_id should still benefit
    from the global + per-agent caps, but the per-task cap can't be
    enforced without something to key off of. Returning a working
    client is the documented graceful degradation.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "0.01")

    # An untracked spend exists in _task_spend under some key, but the
    # contextvar isn't bound — the per-task check shouldn't peek at it.
    record_task_spend(task_id="task-OTHER", agent="historian", cost_usd=10.0)
    # Verify nothing is bound.
    assert "task_id" not in structlog.contextvars.get_contextvars()

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)

    assert isinstance(model, ChatAnthropic)


def test_per_task_cap_falls_back_to_global_and_per_agent_when_task_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task_id missing + per-agent over cap → per-agent still fires.

    Companion to the previous test: missing task_id does NOT bypass the
    other caps, only the per-task one.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1000.0")
    monkeypatch.setenv("COST_CAP_PER_AGENT_DAILY_USD", "1.0")
    monkeypatch.setenv("COST_CAP_PER_TASK_USD", "0.01")

    record_task_spend(task_id="task-X", agent="coder", cost_usd=1.5)

    with (
        patch("agents.llm.service_healthy", return_value=False),
        pytest.raises(CostCapHit) as excinfo,
    ):
        llm("coder", escalate=True)

    assert excinfo.value.cap_kind == "per_agent_daily"


# ---------------------------------------------------------------------------
# record_task_spend / eviction
# ---------------------------------------------------------------------------


def test_record_task_spend_updates_both_accumulators() -> None:
    """A single record_task_spend bumps both per-task + per-agent-daily."""
    record_task_spend(task_id="t1", agent="coder", cost_usd=0.25)
    record_task_spend(task_id="t1", agent="coder", cost_usd=0.75)
    record_task_spend(task_id="t2", agent="coder", cost_usd=0.10)

    assert task_spend_usd("t1") == pytest.approx(1.00)
    assert task_spend_usd("t2") == pytest.approx(0.10)
    assert agent_daily_spend_usd("coder") == pytest.approx(1.10)


def test_record_task_spend_skips_per_task_when_task_id_none() -> None:
    """task_id=None updates only per-agent-daily; no spurious entry created."""
    record_task_spend(task_id=None, agent="historian", cost_usd=0.50)

    assert task_spend_usd(None) == 0.0
    assert agent_daily_spend_usd("historian") == pytest.approx(0.50)


def test_record_task_spend_ignores_zero_and_negative_cost() -> None:
    """Defensive: 0 / negative cost should be a no-op (no entries created)."""
    record_task_spend(task_id="t-zero", agent="coder", cost_usd=0.0)
    record_task_spend(task_id="t-neg", agent="coder", cost_usd=-1.0)

    assert task_spend_usd("t-zero") == 0.0
    assert task_spend_usd("t-neg") == 0.0
    assert agent_daily_spend_usd("coder") == 0.0


def test_evict_old_agent_daily_drops_entries_older_than_retention() -> None:
    """Entries with a UTC date >7d ago are evicted from _agent_daily_spend.

    Seeds the dict directly with stale keys (10d back), a borderline-
    keep (5d back), and today; asserts only the stale one is dropped.
    """
    today = datetime.now(UTC).date()
    stale_key = (today - timedelta(days=10)).isoformat()
    keep_key = (today - timedelta(days=5)).isoformat()
    today_key = today.isoformat()

    _agent_daily_spend[("coder", stale_key)] = 1.0
    _agent_daily_spend[("coder", keep_key)] = 2.0
    _agent_daily_spend[("coder", today_key)] = 3.0

    _evict_old_agent_daily()

    assert ("coder", stale_key) not in _agent_daily_spend
    assert _agent_daily_spend[("coder", keep_key)] == pytest.approx(2.0)
    assert _agent_daily_spend[("coder", today_key)] == pytest.approx(3.0)
