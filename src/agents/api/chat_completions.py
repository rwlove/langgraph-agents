"""POST /v1/chat/completions — OpenAI-compatible chat for OpenWebUI.

Each registered agent appears as a "model" in OpenWebUI. Selecting model
"homelab-engineer" routes the chat directly to that node, bypassing
orchestration. Useful for ad-hoc chat with a specific specialist.

Phase 1: stub. The full implementation lands in phase 7 once we have at least
one specialist authored.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/v1", tags=["openai-compat"])


@router.post("/chat/completions")
async def chat_completions() -> dict[str, str]:
    raise HTTPException(
        status_code=501,
        detail=(
            "chat/completions not yet wired in phase 1. Use /inbox for full "
            "fleet orchestration or wait for phase 7 (OpenWebUI integration)."
        ),
    )
