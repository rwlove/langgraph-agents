"""Pushover client for Tier 1 escalations.

Reuses the same Pushover app token + user key already wired for Alertmanager.
Only emergency-priority Tier 1 sends go through here; Tier 2/3 land in Zulip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from agents.settings import get_settings

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

Priority = Literal[-2, -1, 0, 1, 2]


@dataclass(frozen=True)
class PushoverResult:
    status_code: int
    request_id: str | None


class PushoverError(RuntimeError):
    pass


def send(
    title: str,
    message: str,
    *,
    priority: Priority = 1,
    sound: str = "gamelan",
    url: str | None = None,
    url_title: str | None = None,
    timeout_seconds: float = 10.0,
) -> PushoverResult:
    """Send a Pushover message. Tier 1 default (priority=1, normal sound).

    For wake-the-user emergencies (cost cap hit, security drift, killed task),
    use priority=2 and an `expire` + `retry` schedule — handled by callers
    since they vary by context.
    """
    settings = get_settings()
    if not settings.pushover_app_token or not settings.pushover_user_key:
        raise PushoverError(
            "PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY must be set in env "
            "(via the langgraph-agents-secret ExternalSecret)"
        )

    payload: dict[str, object] = {
        "token": settings.pushover_app_token,
        "user": settings.pushover_user_key,
        "title": title,
        "message": message,
        "priority": priority,
        "sound": sound,
    }
    if url:
        payload["url"] = url
    if url_title:
        payload["url_title"] = url_title

    response = httpx.post(PUSHOVER_API_URL, data=payload, timeout=timeout_seconds)
    if response.status_code >= 400:
        raise PushoverError(f"Pushover send failed: {response.status_code} {response.text[:200]}")

    body = response.json() if response.text else {}
    return PushoverResult(
        status_code=response.status_code,
        request_id=body.get("request") if isinstance(body, dict) else None,
    )
