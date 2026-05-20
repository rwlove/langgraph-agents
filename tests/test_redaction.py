"""Tests for the data-tier emission gate (Phase 3.H).

Pins the HOMELAB-SPEC Layer 5 contract: restricted-tier tasks cannot
emit to remote destinations (Claude, vault summaries, external logs).
"""

from __future__ import annotations

import pytest
import structlog

from agents.redaction import (
    RestrictedTierEmissionBlocked,
    assert_emission_allowed,
    current_data_tier,
)


@pytest.fixture(autouse=True)
def _clear_contextvars():
    """Each test starts with a clean contextvar set."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def test_no_contextvar_defaults_to_internal() -> None:
    assert current_data_tier() == "internal"


def test_bound_contextvar_returned() -> None:
    structlog.contextvars.bind_contextvars(data_tier="restricted")
    assert current_data_tier() == "restricted"


def test_internal_emits_to_claude() -> None:
    structlog.contextvars.bind_contextvars(data_tier="internal")
    assert_emission_allowed("claude")  # no raise


def test_public_emits_to_claude() -> None:
    structlog.contextvars.bind_contextvars(data_tier="public")
    assert_emission_allowed("claude")  # no raise


def test_restricted_blocked_for_claude() -> None:
    structlog.contextvars.bind_contextvars(
        task_id="t-001",
        data_tier="restricted",
    )
    with pytest.raises(RestrictedTierEmissionBlocked) as excinfo:
        assert_emission_allowed("claude")
    assert excinfo.value.destination == "claude"
    assert excinfo.value.task_id == "t-001"


def test_restricted_blocked_for_vault_summary() -> None:
    structlog.contextvars.bind_contextvars(data_tier="restricted")
    with pytest.raises(RestrictedTierEmissionBlocked):
        assert_emission_allowed("vault_summary")


def test_restricted_blocked_for_external_log() -> None:
    structlog.contextvars.bind_contextvars(data_tier="restricted")
    with pytest.raises(RestrictedTierEmissionBlocked):
        assert_emission_allowed("external_log")


def test_explicit_override_allowed_to_pass() -> None:
    """Explicit data_tier kwarg wins over contextvars."""
    structlog.contextvars.bind_contextvars(data_tier="restricted")
    # Caller can force-allow by passing a non-restricted tier explicitly,
    # but the practical use is the opposite: pass `restricted` even when
    # unbound to test fail-closed paths.
    assert_emission_allowed("claude", data_tier="internal")


def test_explicit_restricted_overrides_contextvar() -> None:
    structlog.contextvars.bind_contextvars(data_tier="internal")
    with pytest.raises(RestrictedTierEmissionBlocked):
        assert_emission_allowed("claude", data_tier="restricted")


def test_missing_task_id_still_raises() -> None:
    """Restricted gate fires even without a bound task_id."""
    structlog.contextvars.bind_contextvars(data_tier="restricted")
    with pytest.raises(RestrictedTierEmissionBlocked) as excinfo:
        assert_emission_allowed("claude")
    assert excinfo.value.task_id is None
