"""The real Anthropic SDK against a fake HTTP server: checks request shape and response parsing
without network access or an API key. It does not prove the live API accepts the model name."""
import json

import anthropic
import pytest

try:                      # newer SDK releases ship their own HTTP layer
    import httpx2 as httpx
except ImportError:       # older releases use httpx
    import httpx

from app.agents import AnthropicLLM, LLMError
from app.agents.gateway import tools_for


def sdk(handler):
    return anthropic.Anthropic(api_key="sk-test-secret", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def message(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_request_shape_and_tool_use_parsing():
    seen = {}

    def handler(req):
        seen["path"], seen["key"], seen["body"] = req.url.path, req.headers["x-api-key"], json.loads(req.content)
        return httpx.Response(200, json=message([{"type": "text", "text": "Reading memory."},
                                                 {"type": "tool_use", "id": "toolu_9", "name": "find_derived", "input": {}}],
                                                "tool_use"))

    llm = AnthropicLLM(model="claude-test", max_tokens=321, client=sdk(handler))
    tools = [t.schema() for t in tools_for("analyst")]
    resp = llm.create(system="SYS", messages=[{"role": "user", "content": "hi"}], tools=tools)
    assert seen["path"] == "/v1/messages" and seen["key"] == "sk-test-secret"
    assert seen["body"]["model"] == "claude-test" and seen["body"]["max_tokens"] == 321
    assert seen["body"]["system"] == "SYS" and [t["name"] for t in seen["body"]["tools"]] == [t["name"] for t in tools]
    assert resp.stop_reason == "tool_use" and resp.text == "Reading memory."
    assert resp.tool_calls == [{"type": "tool_use", "id": "toolu_9", "name": "find_derived", "input": {}}]


def test_http_errors_become_llm_errors_without_leaking_the_key():
    llm = AnthropicLLM(client=sdk(lambda req: httpx.Response(500, json={"type": "error", "error": {
        "type": "api_error", "message": "boom"}})))
    with pytest.raises(LLMError) as e:
        llm.create(system="s", messages=[{"role": "user", "content": "x"}], tools=[])
    assert "sk-test-secret" not in str(e.value)


def test_missing_key_is_a_clear_error(monkeypatch):
    from app import config
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config.get_settings(), "anthropic_api_key", None)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicLLM()
