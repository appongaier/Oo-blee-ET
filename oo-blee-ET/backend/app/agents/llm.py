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
        self.model = model or (s.llm_model if s.resolved_provider == "anthropic" else None) or "claude-sonnet-5-5"
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


def _clean_schema_for_gemini(schema: Any) -> Any:
    """Strip OpenAPI / JSON Schema keywords not supported by Gemini's Schema protobuf."""
    if isinstance(schema, dict):
        return {
            k: _clean_schema_for_gemini(v)
            for k, v in schema.items()
            if k not in ("additionalProperties", "additional_properties", "$schema")
        }
    if isinstance(schema, list):
        return [_clean_schema_for_gemini(v) for v in schema]
    return schema


class GeminiLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 max_tokens: int | None = None, client: Any = None):
        s = get_settings()
        self.model = model or (s.llm_model if s.resolved_provider == "gemini" else None) or "gemini-2.5-flash"
        self.max_tokens = max_tokens or s.llm_max_tokens
        if client is None:
            key = api_key or s.gemini_api_key
            if not key:
                raise LLMError("GEMINI_API_KEY is not set; the agents need a real LLM to run")
            from google import genai
            client = genai.Client(api_key=key)
        self.client = client

    def create(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import json
        import uuid
        from google.genai import types

        # Build tools
        func_decls = []
        for t in tools:
            raw_params = t.get("parameters") or t.get("input_schema") or {}
            params = _clean_schema_for_gemini(raw_params)
            func_decls.append(types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=params,
            ))
        genai_tools = [types.Tool(function_declarations=func_decls)] if func_decls else None

        # Build contents from messages
        id_to_name: dict[str, str] = {}
        contents: list[types.Content] = []

        for m in messages:
            role = m["role"]
            content = m["content"]
            parts: list[types.Part] = []

            if isinstance(content, str):
                if content.strip():
                    parts.append(types.Part.from_text(text=content))
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        if item.strip():
                            parts.append(types.Part.from_text(text=item))
                    elif isinstance(item, dict):
                        itype = item.get("type")
                        if itype == "text":
                            txt = item.get("text", "")
                            if txt.strip():
                                parts.append(types.Part.from_text(text=txt))
                        elif itype == "tool_use":
                            call_id = item.get("id")
                            call_name = item.get("name", "")
                            if call_id and call_name:
                                id_to_name[call_id] = call_name
                            part = types.Part.from_function_call(name=call_name, args=dict(item.get("input") or {}))
                            sig = item.get("thought_signature")
                            if sig is not None:
                                part.thought_signature = sig
                            parts.append(part)
                        elif itype == "tool_result":
                            call_id = item.get("tool_use_id", "")
                            tool_name = id_to_name.get(call_id, "tool_result")
                            raw = item.get("content", "")
                            if isinstance(raw, str):
                                try:
                                    parsed = json.loads(raw)
                                    res_dict = parsed if isinstance(parsed, dict) else {"result": parsed}
                                except Exception:
                                    res_dict = {"result": raw}
                            elif isinstance(raw, dict):
                                res_dict = raw
                            else:
                                res_dict = {"result": raw}
                            parts.append(types.Part.from_function_response(name=tool_name, response=res_dict))

            if parts:
                genai_role = "model" if role == "assistant" else "user"
                contents.append(types.Content(role=genai_role, parts=parts))

        config = types.GenerateContentConfig(
            system_instruction=system if system else None,
            tools=genai_tools,
            max_output_tokens=self.max_tokens,
            temperature=0.0,
        )

        models_to_try = [self.model]
        for fallback in ("gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"):
            if fallback not in models_to_try:
                models_to_try.append(fallback)

        import time
        resp = None
        last_exc = None
        for model_name in models_to_try:
            for attempt in range(2):
                try:
                    resp = self.client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=config,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    err = str(exc)
                    if "503" in err or "UNAVAILABLE" in err or "429" in err or "RESOURCE_EXHAUSTED" in err:
                        time.sleep(1.0)
                        continue
                    break
            if resp is not None:
                break

        if resp is None:
            raise LLMError(f"{type(last_exc).__name__}: {str(last_exc)[:200]}") from last_exc

        blocks = []
        has_tool_call = False
        if resp.candidates:
            cand = resp.candidates[0]
            if cand.content and cand.content.parts:
                for part in cand.content.parts:
                    if getattr(part, "text", None):
                        blocks.append({"type": "text", "text": part.text})
                    if getattr(part, "function_call", None):
                        fc = part.function_call
                        has_tool_call = True
                        call_id = getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:8]}"
                        block = {"type": "tool_use", "id": call_id, "name": fc.name, "input": dict(fc.args or {})}
                        sig = getattr(part, "thought_signature", None)
                        if sig is not None:
                            block["thought_signature"] = sig
                        blocks.append(block)

        stop_reason = "tool_use" if has_tool_call else "end_turn"
        return LLMResponse(blocks=blocks, stop_reason=stop_reason)


def get_llm() -> LLM:
    s = get_settings()
    provider = s.resolved_provider
    if provider == "gemini":
        return GeminiLLM()
    if provider == "anthropic":
        return AnthropicLLM()
    raise LLMError("Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY is configured in .env")

