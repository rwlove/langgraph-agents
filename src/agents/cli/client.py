"""HTTP client wrapper for the `hai` CLI.

Thin layer over httpx that injects the Bearer token, handles 401/403
with a friendly hint, and centralizes endpoint construction. Keeps the
command modules tidy.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

from agents.cli.config import Config


class HaiClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        headers: dict[str, str] = {"Accept": "application/json"}
        if config.token is not None:
            headers["Authorization"] = f"Bearer {config.token}"
        self._client = httpx.Client(
            base_url=config.endpoint,
            headers=headers,
            timeout=httpx.Timeout(30.0, read=300.0),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HaiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        try:
            r = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            print(f"error: HTTP failure → {exc}", file=sys.stderr)
            sys.exit(1)
        if r.status_code == 401:
            print(
                "error: 401 Unauthorized. The server requires a Bearer token.\n"
                "Run: hai auth set-token <value>",
                file=sys.stderr,
            )
            sys.exit(1)
        if r.status_code == 403:
            print(
                "error: 403 Forbidden. Token rejected by the server.\n"
                "Verify the value matches the `hai-cli.TOKEN` field in 1Password.",
                file=sys.stderr,
            )
            sys.exit(1)
        if r.status_code >= 400:
            print(
                f"error: {r.status_code} {r.reason_phrase} → {r.text[:400]}",
                file=sys.stderr,
            )
            sys.exit(1)
        result: dict[str, Any] | list[Any] = r.json()
        return result
