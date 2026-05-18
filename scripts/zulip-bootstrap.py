#!/usr/bin/env python3
"""
Idempotently provision the langgraph-agents Zulip identity (10 streams + 14 bots)
per ~/vaults/claude/_meta/stream-permissions-matrix.md.

Usage:
    ZULIP_URL=https://chat.thesteamedcrab.com \
    ZULIP_EMAIL=admin@example.com \
    ZULIP_API_KEY=... \
    python scripts/zulip-bootstrap.py [--apply]

Without --apply, prints a plan and exits. With --apply, creates whatever is missing.
Re-runs are safe: existing streams/bots are reused, not duplicated.

Bot API keys are written to stdout as JSON for one-shot capture into 1Password.
Stream "who-can-post" permission tightening (the matrix's `can_post:` constraint)
is NOT enforced here — Zulip's per-stream posting policy is a coarser knob than
the matrix wants (everyone | admins | full-members | nobody). True per-bot
posting allowlists require an API change upstream or a posting-time check inside
each agent node. Documented in the matrix file under "Open items".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

STREAMS: list[dict[str, str]] = [
    {"name": "homelab", "description": "Cluster, infra, GitOps activity"},
    {"name": "smart-home", "description": "HA, Z-Wave, ESPHome, Frigate"},
    {"name": "property", "description": "3532 Foxhall coordination"},
    {"name": "vehicles", "description": "BMW + Toyota work"},
    {"name": "career", "description": "Resume / LinkedIn / role-search"},
    {"name": "hobby", "description": "Multicade, Kodi, side-projects"},
    {"name": "medical", "description": "Health-tracker drafts — user-only read"},
    {"name": "approvals", "description": "Approval requests + reactions (broker-bot only)"},
    {"name": "supervisor", "description": "Cross-cutting signals, anomalies"},
    {"name": "digests", "description": "Daily/weekly reports"},
]

BOTS: list[dict[str, Any]] = [
    {"id": "triager", "display": "Triager 📥", "subscribe_to": "all-except-medical"},
    {"id": "reporter", "display": "Reporter 📊", "subscribe_to": "all-except-medical"},
    {"id": "note-maker", "display": "Scribe 📝", "subscribe_to": "all-except-medical"},
    {"id": "researcher", "display": "Scout 🔍", "subscribe_to": "all-except-medical"},
    {"id": "coder", "display": "Forge ⚒️", "subscribe_to": "all-except-medical"},
    {"id": "errand-runner", "display": "Runner 🏃", "subscribe_to": "all-except-medical"},
    {"id": "supervisor", "display": "Watchman 🦉", "subscribe_to": "all-except-medical"},
    {"id": "reviewer", "display": "Auditor 🧹", "subscribe_to": "all-except-medical"},
    {"id": "homelab-engineer", "display": "Wrench 🔧", "subscribe_to": "all-except-medical"},
    {"id": "network-operator", "display": "Pylon 🛰️", "subscribe_to": "all-except-medical"},
    {"id": "storage-operator", "display": "Granary 🌾", "subscribe_to": "all-except-medical"},
    {"id": "smart-home-operator", "display": "Sentinel 👁️", "subscribe_to": "all-except-medical"},
    {"id": "ml-operator", "display": "Cortex 🧠", "subscribe_to": "all-except-medical"},
    {"id": "observability-operator", "display": "Beacon 🔦", "subscribe_to": "all-except-medical"},
    {"id": "health-tracker", "display": "Guardian 🩺", "subscribe_to": "medical-only"},
    {"id": "property-coordinator", "display": "Steward 🏡", "subscribe_to": "all-except-medical"},
    {"id": "approval-broker", "display": "Broker 🔒", "subscribe_to": "all-except-medical"},
]


class Zulip:
    def __init__(self, url: str, email: str, api_key: str):
        self.url = url.rstrip("/")
        self.auth = (email, api_key)
        self.s = requests.Session()
        self.s.verify = True

    def _get(self, path: str, **params: Any) -> dict:
        r = self.s.get(f"{self.url}{path}", auth=self.auth, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, **data: Any) -> dict:
        r = self.s.post(f"{self.url}{path}", auth=self.auth, data=data, timeout=30)
        return r.json()

    # ---- streams ----
    def list_streams(self) -> list[dict]:
        return self._get("/api/v1/streams").get("streams", [])

    def ensure_stream(self, name: str, description: str, dry: bool) -> dict:
        existing = {s["name"]: s for s in self.list_streams()}
        if name in existing:
            return {"stream": name, "action": "exists", "id": existing[name]["stream_id"]}
        if dry:
            return {"stream": name, "action": "would-create"}
        # Subscribe self as owner so the stream is created; description follows.
        body = {
            "subscriptions": json.dumps([{"name": name, "description": description}]),
            "invite_only": "true" if name == "medical" else "false",
            "history_public_to_subscribers": "false" if name == "medical" else "true",
        }
        r = self._post("/api/v1/users/me/subscriptions", **body)
        if r.get("result") != "success":
            return {"stream": name, "action": "error", "detail": r}
        return {"stream": name, "action": "created", "result": r}

    # ---- bots ----
    def list_bots(self) -> list[dict]:
        return self._get("/api/v1/users").get("members", [])

    def ensure_bot(self, bot_id: str, display: str, dry: bool) -> dict:
        # Zulip auto-appends "-bot" to short_name when forming the email, so a
        # short_name of "triager" yields email "triager-bot@<realm>". The
        # POST /api/v1/bots response does NOT include the email, so we look it
        # up by user_id after creation.
        all_users = self.list_bots()
        slug = bot_id
        target_email_prefix = f"{slug}-bot@"
        for u in all_users:
            if u.get("is_bot") and u.get("email", "").startswith(target_email_prefix):
                return {
                    "bot": slug,
                    "action": "exists",
                    "email": u["email"],
                    "user_id": u["user_id"],
                }
        if dry:
            return {"bot": slug, "action": "would-create"}
        r = self._post(
            "/api/v1/bots",
            full_name=display,
            short_name=slug,
            bot_type=1,  # generic
        )
        if r.get("result") != "success":
            return {"bot": slug, "action": "error", "detail": r}
        user_id = r["user_id"]
        user = self._get(f"/api/v1/users/{user_id}").get("user", {})
        return {
            "bot": slug,
            "action": "created",
            "email": user.get("email", f"{slug}-bot@unknown"),
            "user_id": user_id,
            "api_key": r["api_key"],
        }

    def subscribe_bot_to_streams(self, bot_email: str, streams: list[str], dry: bool) -> dict:
        if dry:
            return {"bot": bot_email, "subscribe": streams, "action": "would-subscribe"}
        body = {
            "subscriptions": json.dumps([{"name": s} for s in streams]),
            "principals": json.dumps([bot_email]),
        }
        r = self._post("/api/v1/users/me/subscriptions", **body)
        return {"bot": bot_email, "subscribe": streams, "result": r}


def streams_for_bot(bot_subscribe_to: str) -> list[str]:
    if bot_subscribe_to == "medical-only":
        return ["medical"]
    # all-except-medical
    return [s["name"] for s in STREAMS if s["name"] != "medical"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="actually create things (default: dry-run)"
    )
    args = parser.parse_args()
    dry = not args.apply

    url = os.environ["ZULIP_URL"]
    email = os.environ["ZULIP_EMAIL"]
    api_key = os.environ["ZULIP_API_KEY"]
    z = Zulip(url, email, api_key)

    print(f"# Zulip: {url}  (dry-run: {dry})", file=sys.stderr)
    print("# Phase 1: streams", file=sys.stderr)
    for s in STREAMS:
        r = z.ensure_stream(s["name"], s["description"], dry)
        print(f"  {r}", file=sys.stderr)

    print("# Phase 2: bots", file=sys.stderr)
    created_bots: list[dict] = []
    for b in BOTS:
        r = z.ensure_bot(b["id"], b["display"], dry)
        print(f"  {r}", file=sys.stderr)
        if r.get("action") == "created":
            created_bots.append(r)

    print("# Phase 3: subscribe each bot to its streams", file=sys.stderr)
    all_bots = {u["email"]: u for u in z.list_bots() if u.get("is_bot")}
    for b in BOTS:
        # find this bot's email in the current state
        slug = b["id"]
        match = next(
            (u for u in all_bots.values() if u["email"].startswith(f"{slug}-bot@")),
            None,
        )
        if match is None:
            print(
                f"  {{'bot': {slug!r}, 'subscribe': 'skipped — bot not present'}}", file=sys.stderr
            )
            continue
        streams = streams_for_bot(b["subscribe_to"])
        r = z.subscribe_bot_to_streams(match["email"], streams, dry)
        print(f"  {r}", file=sys.stderr)

    # newly-created bot credentials go to stdout for 1Password capture
    if created_bots:
        print(json.dumps(created_bots, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
