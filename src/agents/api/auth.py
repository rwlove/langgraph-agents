"""Bearer-token authentication middleware for the public `hai.<domain>` ingress.

The `hai` CLI POSTs to `/inbox` + `/admin/*` with
`Authorization: Bearer <token>`. This middleware validates against
`settings.hai_cli_token` (sourced from the `hai-cli.TOKEN` field of the
1Password Kubernetes vault via the existing ExternalSecret pipeline).

Scope:

- **Required on**: `/inbox`, `/admin/*`
- **Allowed-through (no auth)**: `/healthz`, `/readyz`, `/metrics`,
  `/approval` (HMAC-signed token in the body, separate auth scheme).
- **Not applicable** to the in-cluster Open WebUI agent-as-model path
  (`/chat/completions`) — that traffic comes via the istio mesh from
  the open-webui pod, never via the public ingress.

When `hai_cli_token` is unset (`None`), the middleware logs a warning
and short-circuits — every request is allowed. This keeps the dev /
local-only mode working until the secret is materialized in the
cluster. Production sets the token via the ExternalSecret.
"""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Paths that REQUIRE Bearer-token auth.
_PROTECTED = re.compile(r"^/(inbox|admin/)")

# Paths explicitly exempt — listed for clarity even though the
# inverse (_PROTECTED) is the gate.
_EXEMPT_PATTERNS = (
    r"^/healthz$",
    r"^/readyz$",
    r"^/metrics$",
    r"^/approval$",
    r"^/chat/completions$",
    r"^/docs",
    r"^/openapi\.json$",
    r"^/$",
)


def _is_protected(path: str) -> bool:
    return _PROTECTED.match(path) is not None


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def hai_cli_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """ASGI middleware — gate /inbox + /admin/* on Bearer-token match.

    Mounted via `app.middleware("http")` in `agents.main`.
    """
    path = request.url.path
    if not _is_protected(path):
        return await call_next(request)

    expected = request.app.state.hai_cli_token
    if expected is None:
        logger.warning(
            "hai_cli_token is unset; allowing %s through unauthenticated. "
            "Set HAI_CLI_TOKEN in production.",
            path,
        )
        return await call_next(request)

    presented = _extract_bearer(request.headers.get("authorization"))
    if presented is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "missing or malformed Authorization header"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison to avoid timing-leak via response latency.
    if not _constant_time_eq(presented, expected):
        return JSONResponse(
            status_code=403,
            content={"detail": "invalid bearer token"},
        )

    return await call_next(request)


def _constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings in constant time. Wraps hmac.compare_digest."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
