"""Tests for the `zulip_base_url` override in `agents.tools.zulip.send_dm`.

In this cluster the public Zulip hostname resolves via split-horizon DNS
to an external LB IP that pods can't reach (Cilium kube-proxy-replacement
quirk). The worker DMs would silently fail with a network error. The
`ZULIP_BASE_URL` env var lets us point the client at the cluster-internal
Service URL (`http://zulip.collab.svc.cluster.local`) instead. These
tests pin the URL construction so a future refactor doesn't accidentally
drop the override.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.settings import get_settings
from agents.tools.zulip import send_dm


def _set_zulip(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "chat.example.com",
    base_url: str | None = None,
) -> None:
    monkeypatch.setenv("ZULIP_HOST", host)
    monkeypatch.setenv("ZULIP_TRIAGER_EMAIL", "triager-bot@example.com")
    monkeypatch.setenv("ZULIP_TRIAGER_API_KEY", "test-api-key")
    if base_url is None:
        monkeypatch.delenv("ZULIP_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("ZULIP_BASE_URL", base_url)
    get_settings.cache_clear()


def test_send_dm_falls_back_to_https_host_when_base_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path: `https://{zulip_host}` (legacy configuration)."""
    _set_zulip(monkeypatch)

    captured: dict[str, str] = {}

    def _capture(url: str, **_kwargs: object) -> MagicMock:
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": 1}
        return resp

    with patch("agents.tools.zulip.httpx.post", side_effect=_capture):
        send_dm(8, "hi")

    assert captured["url"] == "https://chat.example.com/api/v1/messages"


def test_send_dm_uses_base_url_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ZULIP_BASE_URL is set, send_dm targets it directly — host is ignored."""
    _set_zulip(
        monkeypatch,
        host="chat.example.com",  # public-facing; would not route from inside cluster
        base_url="http://zulip.collab.svc.cluster.local",
    )

    captured: dict[str, str] = {}

    def _capture(url: str, **_kwargs: object) -> MagicMock:
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": 2}
        return resp

    with patch("agents.tools.zulip.httpx.post", side_effect=_capture):
        send_dm(8, "hi")

    assert captured["url"] == "http://zulip.collab.svc.cluster.local/api/v1/messages"


def test_send_dm_strips_trailing_slash_on_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing slash on ZULIP_BASE_URL must not produce `//api/v1/...`."""
    _set_zulip(
        monkeypatch,
        base_url="http://zulip.collab.svc.cluster.local/",
    )

    captured: dict[str, str] = {}

    def _capture(url: str, **_kwargs: object) -> MagicMock:
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": 3}
        return resp

    with patch("agents.tools.zulip.httpx.post", side_effect=_capture):
        send_dm(8, "hi")

    assert captured["url"] == "http://zulip.collab.svc.cluster.local/api/v1/messages"
