"""Dual-auth middleware for the public `hai.*` and `hai-web.*` ingresses.

Two auth paths are accepted on `/inbox` + `/admin/*`:

1. **Bearer token** (used by the `hai` CLI). Validated against
   `settings.hai_cli_token` (sourced from the `hai-cli.TOKEN` field of
   the 1Password Kubernetes vault via the existing ExternalSecret
   pipeline). Used by `hai.<domain>` where oauth2-proxy is configured
   with `--skip-auth-route=^/admin/` so the CLI's Bearer reaches the
   backend unmodified.

2. **oauth2-proxy session headers** (used by browser sessions). When
   the request arrives with a non-empty `X-Forwarded-Email` or
   `X-Auth-Request-Email` header, that's oauth2-proxy attesting that
   Authelia accepted the user. Used by `hai-web.<domain>` where
   oauth2-proxy authenticates browser sessions and forwards the
   identity headers to the backend.

Either path admits. If both are absent → 401. If a Bearer is presented
but invalid → 403 (the Bearer takes precedence; failing it doesn't
fall through to the header path, since presenting a wrong Bearer is a
clearer error signal than silently demoting to header-auth).

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

## Threat model on the oauth2-proxy header path

Trust assumption: the header is only set by oauth2-proxy AFTER a
successful Authelia authentication. A request reaching the pod
directly (bypassing oauth2-proxy) could spoof the header — that's
mitigated by network policy restricting `/admin/*` ingress to
oauth2-proxy + intra-namespace + the cluster's mcp / windmill
sources. Acceptable for the homelab threat model; harden via a
shared-secret request signature if the assumption no longer holds.
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


def _extract_oauth2_proxy_email(headers: dict[str, str] | object) -> str | None:
    """Return the user identity oauth2-proxy attests to, or None.

    oauth2-proxy emits one or both of these headers after a successful
    Authelia authentication:

    - ``X-Forwarded-Email`` (the classic header; what most upstream
      installations rely on)
    - ``X-Auth-Request-Email`` (the newer prefix; oauth2-proxy v7+)

    Returns the first non-empty value found, stripped. Returns None
    when neither header is present — the caller treats that as
    "no oauth2-proxy attestation here."
    """
    get = headers.get  # type: ignore[union-attr]
    for h in ("x-forwarded-email", "x-auth-request-email"):
        v = get(h)
        if v and v.strip():
            return v.strip()
    return None


async def hai_cli_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """ASGI middleware — gate /inbox + /admin/* on Bearer-token OR oauth2-proxy session.

    Mounted via `app.middleware("http")` in `agents.main`.

    See module docstring for the dual-auth contract: Bearer takes
    precedence when present; oauth2-proxy header is the fallback.
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
    if presented is not None:
        # Bearer path — verify in constant time.
        if not _constant_time_eq(presented, expected):
            return JSONResponse(
                status_code=403,
                content={"detail": "invalid bearer token"},
            )
        return await call_next(request)

    # No Bearer — check for oauth2-proxy attestation (the hai-web.*
    # ingress route forwards X-Forwarded-Email after Authelia auth).
    oauth_user = _extract_oauth2_proxy_email(request.headers)
    if oauth_user is not None:
        logger.debug("admin admit via oauth2-proxy header user=%s path=%s", oauth_user, path)
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "missing or malformed Authorization header"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings in constant time. Wraps hmac.compare_digest."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
