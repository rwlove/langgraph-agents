"""OpenAI-compatible `/v1/chat/completions` for OpenWebUI integration.

OpenWebUI registers an external OpenAI-compatible endpoint and lists its
models in the model picker. Each registered agent ID is exposed here as
a model:
  - GET /v1/models → list of {id, object, owned_by} for the 15 agents
  - POST /v1/chat/completions → single-agent chat (no fleet orchestration)

This surface deliberately bypasses the triager + approval flow — it's for
ad-hoc direct chat with one specialist. The full orchestration path is
POST /inbox.

Streaming via SSE (Server-Sent Events) is supported. OpenWebUI uses it
to show partial output as the LLM generates.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from agents.personas import load_persona
from agents.settings import get_settings
from agents.state import ALL_AGENT_IDS, AgentId

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


_PER_AGENT_MODEL: dict[str, str] = {
    "triager": "qwen2.5:7b",
    "reporter": "qwen2.5:7b",
    "note-maker": "qwen2.5:7b",
    "researcher": "qwen2.5:7b",
    "coder": "qwen2.5:7b",
    "errand-runner": "qwen2.5:7b",
    "supervisor": "qwen2.5:7b",
    "reviewer": "qwen2.5:7b",
    "homelab-engineer": "qwen2.5:7b",
    "network-operator": "qwen2.5:7b",
    "storage-operator": "qwen2.5:7b",
    "smart-home-operator": "qwen2.5:7b",
    "ml-operator": "qwen2.5:7b",
    "observability-operator": "qwen2.5:7b",
    "health-tracker": "qwen2.5:7b",  # local-only enforced via persona + no provider switch
    "property-coordinator": "qwen2.5:7b",
    "doc-writer": "qwen2.5:7b",  # fills pre-existing gap noted in project_langgraph_specialist_5_places
}


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


def _make_llm(agent_id: AgentId, temperature: float | None) -> ChatOllama:
    settings = get_settings()
    return ChatOllama(
        model=_PER_AGENT_MODEL.get(agent_id, "qwen2.5:7b"),
        base_url=settings.ollama_base_url.removesuffix("/v1"),
        temperature=0.2 if temperature is None else temperature,
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
    model: str, llm: ChatOllama, lc_messages: list[Any]
) -> AsyncIterator[str]:
    """Stream tokens from ollama as OpenAI SSE chunks."""
    async for chunk in llm.astream(lc_messages):
        text = getattr(chunk, "content", "") or ""
        if text:
            yield _chat_completion_chunk(model, text)
    yield _chat_completion_chunk(model, "", finish="stop")
    yield "data: [DONE]\n\n"


@router.post("/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest, _request: Request
) -> Any:
    """Direct chat with a specific agent. Bypasses fleet orchestration.

    `model` must be one of the 15 agent IDs (see GET /v1/models). The
    request flows: persona load → ollama call → response.
    """
    if req.model not in ALL_AGENT_IDS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown model '{req.model}'. Must be one of: "
                f"{', '.join(ALL_AGENT_IDS)}"
            ),
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

    if req.stream:
        return StreamingResponse(
            _stream_response(req.model, llm, lc_messages),
            media_type="text/event-stream",
        )

    # Non-streaming path
    result = await llm.ainvoke(lc_messages)
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
