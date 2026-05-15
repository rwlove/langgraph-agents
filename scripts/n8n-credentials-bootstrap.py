#!/usr/bin/env python3
"""
Idempotently provision the two n8n credentials used by the langgraph workflows:

  - "Pushover - langgraph-agents"   (type pushoverApi, App token)
  - "Zulip - approval-broker bot"   (type httpBasicAuth, bot_email/api_key)

Reads secrets from 1Password via the `op` CLI. Imports into the running n8n
pod via `n8n import:credentials`. Re-imports overwrite existing credentials
that match the stable id (LgPushoverCred1x / LgZulipBrokerCred).

Usage:
    python scripts/n8n-credentials-bootstrap.py [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

PUSHOVER_CRED_ID = "LgPushoverCred1x"
PUSHOVER_CRED_NAME = "Pushover - langgraph-agents"
PUSHOVER_OP_REF = (
    "op://Kubernetes/langgraph-agents/Section_zmrx7lz3gepkhl63mhw6aq5xmy/PUSHOVER_APP_TOKEN"
)

ZULIP_CRED_ID = "LgZulipBrokerCred"
ZULIP_CRED_NAME = "Zulip - approval-broker bot"
ZULIP_EMAIL_OP_REF = "op://Kubernetes/zulip-bots-langgraph/ZULIP_APPROVAL_BROKER_EMAIL"
ZULIP_KEY_OP_REF = "op://Kubernetes/zulip-bots-langgraph/ZULIP_APPROVAL_BROKER_API_KEY"

N8N_NAMESPACE = "home"
N8N_LABEL_SELECTOR = "app.kubernetes.io/name=n8n"


def op_read(ref: str) -> str:
    out = subprocess.check_output(["op", "read", ref], text=True, stdin=subprocess.DEVNULL)
    return out.strip()


def n8n_pod() -> str:
    out = subprocess.check_output(
        [
            "kubectl",
            "-n",
            N8N_NAMESPACE,
            "get",
            "pods",
            "-l",
            N8N_LABEL_SELECTOR,
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return out.strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="actually import (default: dry-run)")
    args = p.parse_args()
    dry = not args.apply

    print(f"dry-run: {dry}", file=sys.stderr)
    pod = n8n_pod()
    print(f"n8n pod: {pod}", file=sys.stderr)

    push_token = op_read(PUSHOVER_OP_REF)
    zulip_email = op_read(ZULIP_EMAIL_OP_REF)
    zulip_key = op_read(ZULIP_KEY_OP_REF)

    creds = [
        {
            "id": PUSHOVER_CRED_ID,
            "name": PUSHOVER_CRED_NAME,
            "type": "pushoverApi",
            "data": {"apiKey": push_token},
        },
        {
            "id": ZULIP_CRED_ID,
            "name": ZULIP_CRED_NAME,
            "type": "httpBasicAuth",
            "data": {"user": zulip_email, "password": zulip_key},
        },
    ]

    if dry:
        # Redact when printing the plan
        for c in creds:
            keys = list(c["data"].keys())
            print(
                f"would import: id={c['id']} name={c['name']} "
                f"type={c['type']} data-keys={keys}"
            )
        return 0

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(creds, tf)
        local_path = tf.name

    pod_path = "/tmp/lg-creds-bootstrap.json"
    try:
        subprocess.check_call(
            [
                "kubectl",
                "-n",
                N8N_NAMESPACE,
                "cp",
                local_path,
                f"{pod}:{pod_path}",
                "-c",
                "app",
            ]
        )
        subprocess.check_call(
            [
                "kubectl",
                "-n",
                N8N_NAMESPACE,
                "exec",
                pod,
                "-c",
                "app",
                "--",
                "n8n",
                "import:credentials",
                f"--input={pod_path}",
            ]
        )
        # best-effort cleanup of the pod-side temp file
        subprocess.run(
            [
                "kubectl",
                "-n",
                N8N_NAMESPACE,
                "exec",
                pod,
                "-c",
                "app",
                "--",
                "rm",
                "-f",
                pod_path,
            ],
            check=False,
        )
    finally:
        os.unlink(local_path)
    print(f"imported {len(creds)} credentials", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
