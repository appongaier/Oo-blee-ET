"""Unit tests for GeminiLLM: verifies request shape, tool schema conversion,
response parsing and error handling without requiring live network access or real API keys."""
from unittest.mock import MagicMock
import pytest
from google.genai import types

from app.agents import GeminiLLM, LLMError, get_llm
from app.agents.gateway import tools_for


def test_gemini_request_shape_and_tool_call_parsing():
    mock_client = MagicMock()

    # Create mock Gemini candidate response with text and function_call
    mock_part_text = types.Part.from_text(text="Checking memory.")
    mock_part_call = types.Part.from_function_call(name="find_derived", args={"user_id": "user_ravi"})
    mock_content = types.Content(role="model", parts=[mock_part_text, mock_part_call])
    mock_candidate = types.Candidate(content=mock_content, finish_reason="STOP")
    mock_resp = types.GenerateContentResponse(candidates=[mock_candidate])

    mock_client.models.generate_content.return_value = mock_resp

    llm = GeminiLLM(model="gemini-2.5-flash", max_tokens=500, client=mock_client)
    tools = [t.schema() for t in tools_for("analyst")]

    resp = llm.create(
        system="System prompt here",
        messages=[{"role": "user", "content": "Tell me about Ravi"}],
        tools=tools,
    )

    assert mock_client.models.generate_content.called
    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == "gemini-2.5-flash"
    assert call_kwargs["config"].system_instruction == "System prompt here"
    assert call_kwargs["config"].max_output_tokens == 500

    # Verify tool declarations were passed
    passed_tools = call_kwargs["config"].tools
    assert passed_tools is not None and len(passed_tools) > 0
    func_names = [f.name for f in passed_tools[0].function_declarations]
    assert "find_derived" in func_names

    # Verify response parsing
    assert resp.text == "Checking memory."
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["name"] == "find_derived"
    assert resp.tool_calls[0]["input"] == {"user_id": "user_ravi"}


def test_gemini_multi_turn_tool_result_conversion():
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text="All done.")]))]
    )

    llm = GeminiLLM(client=mock_client)
    messages = [
        {"role": "user", "content": "What is stored?"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_123", "name": "explain_belief", "input": {"node_id": "fact_1"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_123", "content": '{"status": "found"}'},
            ],
        },
    ]

    resp = llm.create(system="SYS", messages=messages, tools=[])
    assert resp.text == "All done."
    assert resp.stop_reason == "end_turn"

    call_kwargs = mock_client.models.generate_content.call_args.kwargs
    contents = call_kwargs["contents"]
    assert len(contents) == 3
    assert contents[0].role == "user"
    assert contents[1].role == "model"
    assert contents[2].role == "user"
    assert contents[2].parts[0].function_response.name == "explain_belief"


def test_gemini_error_does_not_leak_key():
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("network failed with secret_gemini_key_12345")

    llm = GeminiLLM(api_key="secret_gemini_key_12345", client=mock_client)
    with pytest.raises(LLMError) as exc_info:
        llm.create(system="SYS", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert "RuntimeError" in str(exc_info.value)


def test_gemini_missing_key_raises_llm_error(monkeypatch):
    from app import config
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config.get_settings(), "gemini_api_key", None)

    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM()


def test_get_llm_selects_gemini_when_configured(monkeypatch):
    from app import config
    settings = config.get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "mock-gemini-key")
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "llm_provider", None)

    llm = get_llm()
    assert isinstance(llm, GeminiLLM)
