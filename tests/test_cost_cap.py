"""Tests for the global-daily Claude cost cap (P3.1).

Scope (mirrors the audit + the implementation):

- ``observability.global_claude_spend_usd()`` sums the in-process
  ``langgraph_cost_usd_total`` Counter across every label combination
  where ``group="claude"`` (and ignores local-* spend, which is
  always $0 anyway but should be excluded by the filter).
- ``llm._build_claude`` raises ``CostCapHit`` when that sum is at or
  above ``settings.cost_cap_global_daily_usd``.
- Below-threshold + no spend yet → returns a ``ChatAnthropic`` as
  usual (regression guard so the cap doesn't accidentally block
  every Claude call).

The fixtures reset the Counter via ``_value.set(0)`` per-child rather
than re-registering, because re-creating a Counter with the same name
collides in the default Prom registry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_anthropic import ChatAnthropic

from agents.llm import CostCapHit, llm
from agents.observability import (
    global_claude_spend_usd,
    langgraph_cost_usd_total,
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
    """Clear cached settings + zero the cost Counter between tests."""
    get_settings.cache_clear()
    _reset_cost_counter()
    yield
    get_settings.cache_clear()
    _reset_cost_counter()


def test_global_claude_spend_usd_sums_claude_group_only() -> None:
    """Helper sums only ``group="claude"`` samples; local-* is excluded."""
    langgraph_cost_usd_total.labels(
        agent="coder", group="claude", model="claude-opus-4-7"
    ).inc(2.5)
    langgraph_cost_usd_total.labels(
        agent="reporter", group="claude", model="claude-opus-4-7"
    ).inc(1.25)
    # Local groups don't usually incur cost, but if a downstream emitter
    # ever stamped some, the helper must filter them out.
    langgraph_cost_usd_total.labels(
        agent="triager", group="local-p40", model="qwen2.5:7b"
    ).inc(99.0)

    assert global_claude_spend_usd() == pytest.approx(3.75)


def test_global_claude_spend_usd_zero_when_no_calls() -> None:
    """Fresh process → no Counter children incremented → 0.0."""
    assert global_claude_spend_usd() == 0.0


def test_build_claude_raises_cost_cap_hit_at_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global-daily cap reached → ``CostCapHit`` raised in ``_build_claude``.

    Uses escalate=True so the factory routes to ``_build_claude``
    regardless of local-Ollama health. The cap is set to 1.0 USD and
    the counter is bumped to exactly 1.0 to verify ``>=`` semantics
    (at-threshold is over-budget, not under).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "1.0")

    langgraph_cost_usd_total.labels(
        agent="coder", group="claude", model="claude-opus-4-7"
    ).inc(1.0)

    with patch("agents.llm.service_healthy", return_value=False), pytest.raises(
        CostCapHit
    ) as excinfo:
        llm("coder", escalate=True)

    assert excinfo.value.cap_usd == pytest.approx(1.0)
    assert excinfo.value.accumulated_usd == pytest.approx(1.0)
    assert excinfo.value.agent_id == "coder"


def test_build_claude_raises_cost_cap_hit_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strictly-above-cap also raises (regression guard for ``>`` vs ``>=``)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "5.0")

    langgraph_cost_usd_total.labels(
        agent="reporter", group="claude", model="claude-opus-4-7"
    ).inc(7.5)

    with patch("agents.llm.service_healthy", return_value=False), pytest.raises(
        CostCapHit
    ) as excinfo:
        llm("coder", escalate=True)

    assert excinfo.value.accumulated_usd == pytest.approx(7.5)


def test_build_claude_returns_client_when_below_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spend < cap → factory returns ``ChatAnthropic`` as usual.

    Regression guard: the cap check must not block every Claude call,
    only ones at-or-above the cap.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("COST_CAP_GLOBAL_DAILY_USD", "10.0")

    langgraph_cost_usd_total.labels(
        agent="coder", group="claude", model="claude-opus-4-7"
    ).inc(2.5)

    with patch("agents.llm.service_healthy", return_value=False):
        model = llm("coder", escalate=True)

    assert isinstance(model, ChatAnthropic)
