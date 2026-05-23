"""OpenAI-compatible `/v1/chat/completions` for OpenWebUI integration.

OpenWebUI registers an external OpenAI-compatible endpoint and lists its
models in the model picker. Each registered agent ID is exposed here as
a model:
  - GET /v1/models → list of {id, object, owned_by} for the fleet
  - POST /v1/chat/completions → single-agent chat (no fleet orchestration)

This surface deliberately bypasses the triager + approval flow — it's for
ad-hoc direct chat with one agent. The full orchestration path is
POST /inbox.

The bypass is documented and accepted. The actual chat-model is built
through the shared per-agent factory in ``agents.llm`` (same routing rules
as /inbox + scheduled-graph paths), which attaches the Prom metrics
callback and the Langfuse callback intrinsically. We pass
``trigger="openwebui"`` so the dashboard panel filtering on that label
keeps working. A Class-A entry is also written to the calling agent's
vault activity log on each invocation for audit value.

Streaming via SSE (Server-Sent Events) is supported. OpenWebUI uses it
to show partial output as the LLM generates.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from agents.llm import llm as build_llm
from agents.personas import load_persona
from agents.state import ALL_AGENT_IDS, AgentId
from agents.tools.activity_log import log_activity

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger("agents.api.chat_completions")

router = APIRouter(prefix="/v1", tags=["openai-compat"])


# ---- OpenAI-shaped schemas ----


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str  # = agent_id
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


# ---- /v1/models ----


@router.get("/models")
async def list_models() -> dict[str, Any]:
    """OpenAI `/v1/models` shape. One entry per fleet agent."""
    return {
        "object": "list",
        "data": [
            {
                "id": agent_id,
                "object": "model",
                "owned_by": "langgraph-agents",
            }
            for agent_id in ALL_AGENT_IDS
        ],
    }


# ---- /v1/chat/completions ----


def _to_lc_messages(messages: list[ChatMessage], persona: str) -> list[Any]:
    """Convert OpenAI-shape messages to LangChain message objects.

    Prepends the agent's persona as the first SystemMessage. If the caller
    also provided a system message, both are sent (persona first).
    """
    out: list[Any] = [SystemMessage(content=persona)]
    for m in messages:
        if m.role == "system":
            out.append(SystemMessage(content=m.content))
        elif m.role == "user":
            out.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
        # "tool" role isn't used in this surface — drop silently
    return out


def _make_llm(agent_id: AgentId, temperature: float | None) -> BaseChatModel:
    """Build the chat model for a direct OpenWebUI request.

    Delegates to ``agents.llm.llm(agent_id, ...)`` so this surface picks up
    the same per-agent P40/Spark routing as /inbox + scheduled-graph paths.
    Previously this constructed ``ChatOllama`` directly against
    ``settings.ollama_base_url`` with a local per-agent model dict, which
    silently routed every model (including the Spark-class agents like
    historian / supervisor / coder) to P40 with qwen2.5:7b.

    The factory attaches both ``LangGraphMetricsCallback`` and the
    Langfuse callback intrinsically — see ``observability.py`` for why
    intrinsic-not-with_config is the only reliable path once chains and
    structured-output wrappers come into play.

    ``trigger="openwebui"`` is propagated to the metrics callback's
    ``trigger`` label so the Grafana panel filtering on
    ``trigger=openwebui`` keeps working after this refactor.
    """
    return build_llm(
        agent_id,
        temperature=0.2 if temperature is None else temperature,
        trigger="openwebui",
    )


def _chat_completion_chunk(model: str, content: str, finish: str | None = None) -> str:
    """Render an OpenAI SSE chunk."""
    payload = {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": ({"content": content} if content else {}),
                "finish_reason": finish,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_response(
    model: str, llm: BaseChatModel, lc_messages: list[Any]
) -> AsyncIterator[str]:
    """Stream tokens from the chat model as OpenAI SSE chunks.

    ``BaseChatModel.astream`` is the LangChain interface common to both
    ChatOllama and ChatAnthropic, so this works regardless of which
    backend the factory routed to.
    """
    async for chunk in llm.astream(lc_messages):
        text = getattr(chunk, "content", "") or ""
        if text:
            yield _chat_completion_chunk(model, text)
    yield _chat_completion_chunk(model, "", finish="stop")
    yield "data: [DONE]\n\n"


@router.post("/chat/completions")
async def chat_completions(req: ChatCompletionRequest, _request: Request) -> Any:
    """Direct chat with a specific agent. Bypasses fleet orchestration.

    `model` must be one of the 15 agent IDs (see GET /v1/models). The
    request flows: persona load → ollama call → response.
    """
    if req.model not in ALL_AGENT_IDS:
        raise HTTPException(
            status_code=404,
            detail=(f"unknown model '{req.model}'. Must be one of: {', '.join(ALL_AGENT_IDS)}"),
        )
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty")

    agent_id: AgentId = req.model
    try:
        persona = load_persona(agent_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"persona for '{agent_id}' not loaded yet — push vault content "
                f"via sync-receiver first: {exc}"
            ),
        ) from exc

    llm = _make_llm(agent_id, req.temperature)
    lc_messages = _to_lc_messages(req.messages, persona)

    # OpenWebUI doesn't carry a task_id; mint one so the activity log + Prom
    # labels can stitch this back together if anyone audits the trail.
    task_id = f"openwebui-{uuid.uuid4().hex[:12]}"
    last_user = next(
        (m.content for m in reversed(req.messages) if m.role == "user"),
        "",
    )
    summary = (last_user or "(no user prompt)")[:200]

    def _audit(outcome: str) -> None:
        # Best-effort: a vault write failure (PVC unmounted, perms) must not
        # break the user-facing chat. Mirrors the pattern in fleet.py.
        try:
            log_activity(
                agent_id,
                task_id,
                action_class="A",
                summary=f"[openwebui] {summary}",
                outcome=outcome,
            )
        except Exception as exc:
            logger.warning("openwebui activity log write failed: %s", exc)

    if req.stream:
        # Audit the stream as success on start — we can't easily distinguish
        # mid-stream errors without a wrapping generator. The metrics
        # callback's on_llm_error fires either way for the Prom side.
        _audit("success")
        return StreamingResponse(
            _stream_response(req.model, llm, lc_messages),
            media_type="text/event-stream",
        )

    # Non-streaming path
    try:
        result = await llm.ainvoke(lc_messages)
    except Exception:
        _audit("error")
        raise
    _audit("success")
    text = getattr(result, "content", "") or ""
    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,  # ollama doesn't report; populate from response if available
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
