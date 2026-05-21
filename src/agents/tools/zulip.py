"""Zulip DM client for the triager-bot reply-back path.

When /inbox is called from the Zulip outgoing-webhook (Windmill's
`zulip-triager-webhook` adapter), the bot posts a fast dispatch ack
and disconnects. The fleet graph then runs to completion (or pauses
on approval). When it does, we POST the final output back to the
same DM thread using the triager-bot's Zulip API key — so the user
sees the answer land in the same conversation they started.

Best-effort: failures here (network, auth, Zulip down) get logged
but never raise; the graph already produced its output, we just
couldn't surface it via this channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from agents.settings import get_settings

logger = logging.getLogger("agents.tools.zulip")


@dataclass(frozen=True)
class ZulipDMResult:
    status_code: int
    msg_id: int | None


class ZulipNotConfiguredError(RuntimeError):
    """Settings are missing — caller should treat as no-op, not error."""


def send_dm(
    user_id: int,
    content: str,
    *,
    timeout_seconds: float = 10.0,
) -> ZulipDMResult:
    """Post a direct message to a Zulip user as triager-bot.

    `user_id` is the Zulip numeric user id (e.g. 8 for rob). Pass the
    `message.sender_id` value from the outgoing-webhook payload — this
    keeps the conversation threaded with whoever originally triggered
    the task.

    Raises `ZulipNotConfiguredError` if the env isn't set (settings
    fields zulip_host / zulip_triager_email / zulip_triager_api_key).
    Callers in best-effort paths should catch + log this without
    raising further.

    Other failures (HTTP non-2xx, network) are returned as a
    ZulipDMResult with the actual status_code so callers can decide
    whether to retry or log.
    """
    settings = get_settings()
    if (
        not settings.zulip_host
        or not settings.zulip_triager_email
        or not settings.zulip_triager_api_key
    ):
        raise ZulipNotConfiguredError(
            "ZULIP_HOST, ZULIP_TRIAGER_EMAIL, and ZULIP_TRIAGER_API_KEY "
            "must all be set (via the langgraph-agents-secret ExternalSecret) "
            "for /inbox to post replies back to Zulip."
        )

    # Prefer the explicit base URL when set — needed in clusters where
    # the public Zulip hostname resolves to an LB IP pods can't reach
    # (Cilium kube-proxy-replacement / split-horizon DNS). Falls back to
    # `https://{zulip_host}` for the legacy host-only configuration.
    base_url = (
        settings.zulip_base_url.rstrip("/")
        if settings.zulip_base_url
        else f"https://{settings.zulip_host}"
    )
    url = f"{base_url}/api/v1/messages"
    # Zulip's REST API uses HTTP Basic auth: <bot_email>:<api_key>.
    # type=direct + to=[user_id] sends a DM that lands in the existing
    # 1:1 thread with that user (same thread the bot was DM'd from).
    auth = (settings.zulip_triager_email, settings.zulip_triager_api_key)
    data = {
        "type": "direct",
        "to": f"[{user_id}]",
        "content": content,
    }

    # When `zulip_base_url` overrides the public hostname for routing
    # (cluster-internal Service URL), Zulip's Django backend still
    # checks Host against ALLOWED_HOSTS — which lists the public
    # hostname, not the in-cluster Service name. Without this override
    # the request gets Django's bare 400 HTML page. Set Host explicitly
    # so the public hostname appears in the request even though the
    # TCP connection terminates at the in-cluster Service.
    headers: dict[str, str] = {}
    if settings.zulip_base_url and settings.zulip_host:
        headers["Host"] = settings.zulip_host

    try:
        resp = httpx.post(
            url,
            auth=auth,
            data=data,
            headers=headers,
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        logger.warning("zulip DM failed (network): %s", exc)
        return ZulipDMResult(status_code=0, msg_id=None)

    if resp.status_code != 200:
        logger.warning(
            "zulip DM rejected: status=%s body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return ZulipDMResult(status_code=resp.status_code, msg_id=None)

    body = resp.json()
    return ZulipDMResult(status_code=200, msg_id=body.get("id"))
