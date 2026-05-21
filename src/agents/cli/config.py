"""Config + auth-token loading for the `hai` CLI.

Config lives at `~/.config/hai/config.toml`:

    endpoint = "https://hai.example.com"
    # Optional — defaults shown below.
    poll_interval_s = 2
    poll_timeout_s  = 600

Token lives at `~/.config/hai/token` (chmod 600). Set via
`hai auth set-token <value>`. Plaintext; the file's permission bits
are the only protection.

Both files are created lazily — `hai task add` will tell the user
to run `hai auth set-token` if either is missing.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("HAI_CONFIG_DIR", Path.home() / ".config" / "hai"))
CONFIG_FILE = CONFIG_DIR / "config.toml"
TOKEN_FILE = CONFIG_DIR / "token"


@dataclass(frozen=True)
class Config:
    endpoint: str
    token: str | None
    poll_interval_s: float
    poll_timeout_s: float


def load_config() -> Config:
    """Load config + token. Returns a Config with `token=None` if missing.

    Callers that need auth should check `cfg.token is None` and prompt
    the user to run `hai auth set-token` rather than crashing.
    """
    endpoint = os.environ.get("HAI_ENDPOINT")
    poll_interval = float(os.environ.get("HAI_POLL_INTERVAL_S", "2"))
    poll_timeout = float(os.environ.get("HAI_POLL_TIMEOUT_S", "600"))

    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            data = tomllib.load(f)
        if endpoint is None:
            endpoint = data.get("endpoint")
        poll_interval = float(data.get("poll_interval_s", poll_interval))
        poll_timeout = float(data.get("poll_timeout_s", poll_timeout))

    if endpoint is None:
        print(
            "error: HAI_ENDPOINT not set and ~/.config/hai/config.toml is missing.\n"
            "Create it with:\n"
            '  echo \'endpoint = "https://hai.<your-domain>"\' '
            f"> {CONFIG_FILE}",
            file=sys.stderr,
        )
        sys.exit(2)

    token = None
    env_token = os.environ.get("HAI_TOKEN")
    if env_token:
        token = env_token
    elif TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if token == "":
        token = None

    return Config(
        endpoint=endpoint.rstrip("/"),
        token=token,
        poll_interval_s=poll_interval,
        poll_timeout_s=poll_timeout,
    )


def write_token(value: str) -> None:
    """Persist a token to ~/.config/hai/token with 0600."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(value.strip() + "\n", encoding="utf-8")
    TOKEN_FILE.chmod(0o600)


def write_default_config(endpoint: str) -> None:
    """Write a baseline config.toml if none exists. Idempotent on content."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        return
    CONFIG_FILE.write_text(
        f'endpoint = "{endpoint}"\n'
        "# poll_interval_s = 2\n"
        "# poll_timeout_s = 600\n",
        encoding="utf-8",
    )
