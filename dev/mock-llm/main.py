"""Fake OpenAI-compatible chat completions endpoint for local development.

Returns scripted tool-call sequences so the PydanticAI agent loop can be
exercised end-to-end without any real model. Implements POST /v1/chat/completions
in non-streaming form only.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from script import respond

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mock-llm")

app = FastAPI(title="mock-llm", description="Scripted OpenAI-compatible completions for local dev")


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    stream: bool = False
    temperature: float | None = None


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest) -> dict[str, Any]:
    log.info("messages_count=%d stream=%s", len(request.messages), request.stream)
    choice = respond(request.messages)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model or "mock-llm",
        "choices": [{"index": 0, **choice}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
