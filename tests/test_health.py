"""Tests for `agents.health.service_healthy` — TTL-cached Ollama probe."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.health import reset_cache, service_healthy


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_cache()


def _ok_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    return r


def _bad_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 503
    return r


def test_returns_true_on_200() -> None:
    with patch("httpx.get", return_value=_ok_response()):
        assert service_healthy("http://x:11434") is True


def test_returns_false_on_non_200() -> None:
    with patch("httpx.get", return_value=_bad_response()):
        assert service_healthy("http://x:11434") is False


def test_returns_false_on_connect_error() -> None:
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        assert service_healthy("http://x:11434") is False


def test_returns_false_on_timeout() -> None:
    with patch("httpx.get", side_effect=httpx.ReadTimeout("slow")):
        assert service_healthy("http://x:11434") is False


def test_caches_result_within_ttl() -> None:
    """Two calls within the TTL hit httpx exactly once."""
    with patch("httpx.get", return_value=_ok_response()) as m:
        service_healthy("http://x:11434", ttl_seconds=60.0)
        service_healthy("http://x:11434", ttl_seconds=60.0)
        assert m.call_count == 1


def test_different_urls_cache_independently() -> None:
    """Two URLs each hit httpx once, not once total."""
    with patch("httpx.get", return_value=_ok_response()) as m:
        service_healthy("http://a:11434")
        service_healthy("http://b:11434")
        assert m.call_count == 2


def test_reset_cache_forces_re_probe() -> None:
    with patch("httpx.get", return_value=_ok_response()) as m:
        service_healthy("http://x:11434")
        reset_cache()
        service_healthy("http://x:11434")
        assert m.call_count == 2


def test_uses_correct_endpoint_path() -> None:
    """Probe hits /api/tags (cheap; returns model list)."""
    with patch("httpx.get", return_value=_ok_response()) as m:
        service_healthy("http://x:11434")
        m.assert_called_once()
        assert m.call_args.args[0] == "http://x:11434/api/tags"


def test_strips_trailing_slash() -> None:
    with patch("httpx.get", return_value=_ok_response()) as m:
        service_healthy("http://x:11434/")
        assert m.call_args.args[0] == "http://x:11434/api/tags"
