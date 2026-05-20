"""Idempotency-key dedup store for `/inbox`.

Per HOMELAB-SPEC Layer 5 (task envelope must be safe under at-least-once
delivery) and the rollout plan's Phase 3.G. Backed by Dragonfly because
SET NX EX is one round-trip and the substrate is already deployed.

Behavior:
- `check_and_set(key, value, ttl)` returns the cached value on hit,
  `None` on miss (and sets the value with TTL).
- Hit = the caller's `idempotency_key` matched an existing entry. The
  cached value is whatever the prior call stored — typically the
  prior `task_id`.
- Miss = first time we've seen this key within the TTL window.

Failure mode:
- Dragonfly unreachable → log a warning and return `None`. We do NOT
  block `/inbox` on a sidecar dedup store; better to under-dedup than
  to fail valid requests because the cache is down.

Configuration (via Settings):
- `dragonfly_url` — `redis://host:port/db`. Default
  `redis://dragonfly.databases.svc.cluster.local:6379/0`.
- `idempotency_ttl_seconds` — dedup window. Default 3600 (1 hour, per
  the rollout decision recorded 2026-05-20).
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from agents.observability import get_logger

_KEY_PREFIX = "inbox:idempotency:"

logger = logging.getLogger("agents.idempotency")
slog = get_logger("idempotency")


class DedupStore:
    """Async dedup store backed by Redis-protocol (Dragonfly).

    Construct once at app-startup; share across requests. The underlying
    `redis.asyncio.Redis` client is connection-pooled internally.
    """

    def __init__(self, url: str, default_ttl_seconds: int) -> None:
        self._url = url
        self._default_ttl = default_ttl_seconds
        self._client: Redis | None = None

    async def _client_or_none(self) -> Redis | None:
        """Lazy connection — never raises. Returns None if connect fails."""
        if self._client is not None:
            return self._client
        try:
            client = Redis.from_url(
                self._url,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
                decode_responses=True,
            )
            # PING here to fail fast on misconfig rather than at first SET.
            # Mypy ignore: redis-py types its sync+async Redis class with
            # a union return on ping(); the asyncio variant is awaitable.
            await client.ping()  # type: ignore[misc]
            self._client = client
            return client
        except Exception as exc:
            slog.warning(
                "dedup_store_unavailable",
                url=self._url,
                error=str(exc),
            )
            return None

    async def check_and_set(
        self,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> str | None:
        """SET NX EX semantics.

        Returns:
            - `None` if this is the first time we've seen `key` in the
              dedup window (and the new value was stored).
            - The previously-stored value (a string) if `key` is already
              cached. Caller should treat this as a duplicate request.

        On any client/transport failure: logs a warning, returns `None`.
        The caller proceeds as if the request is novel — which is the
        documented graceful-degradation path.
        """
        client = await self._client_or_none()
        if client is None:
            return None

        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        full_key = f"{_KEY_PREFIX}{key}"

        try:
            # SET NX EX in one round-trip. Returns True on store, None
            # on miss-because-exists. redis-py returns the literal
            # truthy/falsy from the protocol.
            stored = await client.set(full_key, value, nx=True, ex=ttl)
            if stored:
                # First time we've seen this key. Stored; not a duplicate.
                return None
            # Already exists — return the cached value.
            cached = await client.get(full_key)
            return cached if cached is not None else ""
        except Exception as exc:
            slog.warning(
                "dedup_store_op_failed",
                key=key,
                error=str(exc),
            )
            return None

    async def close(self) -> None:
        """Release the connection pool. Best-effort."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
