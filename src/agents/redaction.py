"""Data-tier-driven emission gates (Phase 3.H).

Per HOMELAB-SPEC Layer 5 (Data classification — restricted-tier values
are never summarized, never indexed for retrieval, never emitted to
remote models) and `.agents/instructions/data-classification.md`.

This module is the runtime enforcement layer for the `data_tier` field
landed on the task envelope in 2.F. The convention names *what* the
tiers mean; this module makes *restricted* operative — calls to remote
models and writes to vault summaries fail-closed when the task is
flagged restricted.

Hook points:

- `agents.llm._build_claude` calls `assert_emission_allowed("claude", ...)`
  before constructing the ChatAnthropic client. A restricted task that
  tries to escalate raises `RestrictedTierEmissionBlocked` instead of
  shipping prompt content off-cluster.
- The historian / daily-digest write paths should call
  `assert_emission_allowed("vault_summary", ...)` before flushing to
  `~/vaults/...`. (Wiring those paths is a follow-up — this module is
  scoped to the LLM gate first; the digest path is shorter and lands
  next.)

The data_tier is read from structlog contextvars at the call site —
same pattern the per-task cost cap uses. `/inbox` binds it on the
task's contextvar set; node wrappers preserve the binding across
asyncio task boundaries.

If no `data_tier` is bound, the gate defaults to allow (assumes
`internal` per the envelope default). Old callers that don't pass the
field continue to work; restricted enforcement applies only when a
caller has explicitly tagged the request.
"""

from __future__ import annotations

from typing import Literal

import structlog

EmissionDestination = Literal[
    "claude",
    "vault_summary",
    "external_log",
]


class RestrictedTierEmissionBlocked(RuntimeError):
    """A restricted-tier task tried to emit to a remote / external destination."""

    def __init__(
        self,
        destination: EmissionDestination,
        task_id: str | None = None,
    ) -> None:
        self.destination = destination
        self.task_id = task_id
        super().__init__(
            f"restricted-tier task (task_id={task_id!r}) cannot emit to "
            f"destination={destination!r} per HOMELAB-SPEC Layer 5"
        )


def current_data_tier() -> str:
    """Return the data tier bound on the current asyncio task, or 'internal'.

    Read from structlog contextvars — `/inbox` binds `data_tier` when a
    request comes in with the envelope field set; node wrappers
    preserve the binding through the graph.
    """
    bound = structlog.contextvars.get_contextvars().get("data_tier")
    if isinstance(bound, str):
        return bound
    return "internal"


def assert_emission_allowed(
    destination: EmissionDestination,
    *,
    data_tier: str | None = None,
) -> None:
    """Raise `RestrictedTierEmissionBlocked` if the destination is off-limits.

    Args:
        destination: where the about-to-emit content is headed.
            "claude" — remote Anthropic API
            "vault_summary" — published / indexed summary file in the vault
            "external_log" — any third-party log destination
        data_tier: explicit tier override. If None, read from contextvars.
    """
    tier = data_tier if data_tier is not None else current_data_tier()
    if tier == "restricted":
        bound_task_id = structlog.contextvars.get_contextvars().get("task_id")
        task_id = bound_task_id if isinstance(bound_task_id, str) else None
        raise RestrictedTierEmissionBlocked(
            destination=destination,
            task_id=task_id,
        )
