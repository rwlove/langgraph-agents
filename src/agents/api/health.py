"""Health probes for k8s."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    # Phase 1: graph compile success implies ready. Phase 4+ adds checks for
    # Postgres connectivity, ollama reachability, vault PVC mount.
    return {"status": "ready"}
