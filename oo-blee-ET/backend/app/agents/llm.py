"""Thin LLM layer. Agents talk to the LLM only through this interface, so the real Anthropic
client can be swapped for a scripted one in tests. The LLM never touches the graph directly."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import get_settings


class LLMError(Exception):
    """The model could not be reached or returned something unusable."""


@dataclass
class LLMResponse:
    blocks: list[dict] = field(default_factory=list)   # {"type": "text"|"tool_use", ...}
    stop_reason: str = "end_turn"

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.blocks if b["type"] == "text").strip()

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.blocks if b["type"] == "tool_use"]


class LLM(Protocol):
    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 max_tokens: int | None = None, client: Any = None):
        s = get_settings()
        self.model = model or s.llm_model
        self.max_tokens = max_tokens or s.llm_max_tokens
        if client is None:
            key = api_key or s.anthropic_api_key
            if not key:
                raise LLMError("ANTHROPIC_API_KEY is not set; the agents need a real LLM to run")
            import anthropic
            client = anthropic.Anthropic(api_key=key)
        self.client = client

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        try:
            resp = self.client.messages.create(model=self.model, max_tokens=self.max_tokens,
                                               system=system, messages=messages, tools=tools)
        except Exception as exc:                      # SDK/network errors; never echo the key
            raise LLMError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
        return LLMResponse(blocks=blocks, stop_reason=resp.stop_reason or "end_turn")
