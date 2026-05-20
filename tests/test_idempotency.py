"""Tests for the idempotency dedup store.

The store is intentionally degradation-tolerant: a Redis/Dragonfly outage
must NOT block /inbox. These tests pin that contract.
"""

from __future__ import annotations

import pytest

from agents.idempotency import DedupStore


async def test_dedup_store_unreachable_returns_none() -> None:
    """If Dragonfly is unreachable, check_and_set returns None.

    The /inbox handler interprets None as "not a duplicate, proceed
    normally" — graceful degradation rather than blocking the request.
    """
    # Bogus port; no server. Connect will fail.
    store = DedupStore(
        url="redis://127.0.0.1:1/0",
        default_ttl_seconds=3600,
    )
    result = await store.check_and_set(key="any", value="task-001")
    assert result is None


async def test_dedup_store_close_idempotent() -> None:
    """close() is safe to call without a successful connection."""
    store = DedupStore(
        url="redis://127.0.0.1:1/0",
        default_ttl_seconds=3600,
    )
    # No client constructed → close should not raise.
    await store.close()
    # Calling close twice is also fine.
    await store.close()


async def test_dedup_store_default_ttl_used() -> None:
    """If no ttl is passed to check_and_set, the constructor default is used."""
    store = DedupStore(
        url="redis://127.0.0.1:1/0",
        default_ttl_seconds=3600,
    )
    # Without an active connection we can't verify the ttl on the wire,
    # but the call shouldn't raise — exercise the path.
    result = await store.check_and_set(key="k", value="v")
    assert result is None
    result_with_ttl = await store.check_and_set(key="k", value="v", ttl_seconds=60)
    assert result_with_ttl is None


@pytest.mark.parametrize("ttl", [1, 60, 3600, 86400])
async def test_dedup_store_accepts_various_ttl(ttl: int) -> None:
    """Any positive ttl is accepted; no schema enforcement at the call site."""
    store = DedupStore(
        url="redis://127.0.0.1:1/0",
        default_ttl_seconds=3600,
    )
    result = await store.check_and_set(key="k", value="v", ttl_seconds=ttl)
    assert result is None
