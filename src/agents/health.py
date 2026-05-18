"""Service-health probes for the LLM factory.

Each Ollama Service is probed via `GET /api/tags` with a tight connect+read
timeout. Result is cached per-URL for 60s so a hot-path doesn't pay the probe
cost on every llm() call.

Why 2s connect + 3s read:
  httpx's defaults are 5s connect and no read timeout. With the default,
  a Service whose endpoint pod is gone can take 5-10s before the factory's
  fallback decision fires — meaning the user sees latency on every call until
  the cache catches up. 2s+3s keeps the worst-case fallback decision under 6s.
"""

from __future__ import annotations

import time
from threading import Lock

import httpx

_CONNECT_TIMEOUT = 2.0  # seconds
_READ_TIMEOUT = 3.0
_DEFAULT_TTL = 60.0

_CACHE: dict[str, tuple[float, bool]] = {}
_LOCK = Lock()


def service_healthy(base_url: str, *, ttl_seconds: float = _DEFAULT_TTL) -> bool:
    """Return True if `base_url` responds 200 to GET /api/tags within timeout.

    Cached for `ttl_seconds` per base_url. Cache is process-local — sufficient
    for a single FastAPI worker. If we ever go multi-worker, replace with a
    shared cache (Redis / Postgres) or accept N independent caches.
    """
    base_url = base_url.rstrip("/")
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(base_url)
        if cached is not None and (now - cached[0]) < ttl_seconds:
            return cached[1]

    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT,
        read=_READ_TIMEOUT,
        write=_READ_TIMEOUT,
        pool=_READ_TIMEOUT,
    )
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=timeout)
        healthy = resp.status_code == 200
    except (httpx.HTTPError, OSError):
        healthy = False

    with _LOCK:
        _CACHE[base_url] = (now, healthy)
    return healthy


def reset_cache() -> None:
    """Clear the cache. Tests call this between cases."""
    with _LOCK:
        _CACHE.clear()


__all__ = ["reset_cache", "service_healthy"]
