"""Offline wire regressions for the official Flash ID and retained aliases."""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

from tradingagents.agents.utils.structured import bind_required_structured
from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import OpenAIClient
from tradingagents.llm_clients.validators import validate_model

FLASH_IDS = ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize("model", (*FLASH_IDS, "deepseek-v4-pro"))
def test_required_finalizer_sends_json_without_forced_tool_choice(model):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == model
        assert body.get("response_format") == {"type": "json_object"}
        assert "tool_choice" not in body
        assert "tools" not in body
        assert "Return JSON only" in body["messages"][-1]["content"]
        return httpx.Response(200, json={
            "id": "offline-finalizer", "object": "chat.completion", "created": 1,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"answer":"retained evidence"}',
                "reasoning_content": "synthetic finalizer reasoning",
            }}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        llm = OpenAIClient(model, provider="deepseek", api_key="offline-test",
                           http_client=http_client, max_retries=0).get_llm()
        binding = bind_required_structured(llm, Answer, "Offline Finalizer")
        result = binding.invoke([HumanMessage(content="Summarize retained evidence.")])

    assert result["parsed"] == Answer(answer="retained evidence")
    assert result["raw"].additional_kwargs["reasoning_content"] == "synthetic finalizer reasoning"
    assert result["parsing_error"] is None
    assert len(requests) == 1


def test_flash_menu_uses_canonical_id_and_keeps_pro_distinct():
    for mode in ("quick", "deep"):
        options = {value: label for label, value in get_model_options("deepseek", mode)}
        assert "V4.1" in options["deepseek-flash"]
    assert get_model_options("deepseek", "deep")[0][1] == "deepseek-v4-pro"


def test_flash_aliases_are_valid_and_have_thinking_capabilities():
    for model in FLASH_IDS:
        assert validate_model("deepseek", model)
        caps = get_capabilities(model)
        assert caps.supports_tool_choice is False
        assert caps.requires_reasoning_content_roundtrip is True
        assert caps.supports_json_mode is True


def test_canonical_flash_rejects_forced_tool_choice():
    assert get_capabilities("deepseek-flash").supports_tool_choice is False


@pytest.mark.parametrize("model", FLASH_IDS)
def test_tool_roundtrip_retains_reasoning_including_non_tool_turns(model):
    requests = []
    replies = [
        {"content": "Prior answer", "reasoning_content": "prior non-tool reasoning"},
        {"content": "", "reasoning_content": "tool selection reasoning", "tool_calls": [{
            "id": "offline-call", "type": "function",
            "function": {"name": "lookup", "arguments": '{"symbol":"TEST"}'},
        }]},
        {"content": "Tool evidence summarized", "reasoning_content": "after tool reasoning"},
    ]

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        reply = replies[len(requests) - 1]
        return httpx.Response(200, json={
            "id": "offline-tool", "object": "chat.completion", "created": 1,
            "model": model,
            "choices": [{"index": 0,
                         "finish_reason": "tool_calls" if "tool_calls" in reply else "stop",
                         "message": {"role": "assistant", **reply}}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        llm = OpenAIClient(model, provider="deepseek", api_key="offline-test",
                           http_client=http_client, max_retries=0).get_llm()
        history = [HumanMessage(content="First question")]
        history.append(llm.invoke(history))
        history.append(HumanMessage(content="Look up TEST"))
        bound = llm.bind_tools([{
            "type": "function", "function": {"name": "lookup", "description": "Fixture lookup",
                "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}},
                               "required": ["symbol"]}},
        }])
        tool_request = bound.invoke(history)
        assert tool_request.tool_calls[0]["args"] == {"symbol": "TEST"}
        history.extend([tool_request, ToolMessage(content="synthetic evidence", tool_call_id="offline-call")])
        answer = bound.invoke(history)

    assert answer.content == "Tool evidence summarized"
    assert answer.additional_kwargs["reasoning_content"] == "after tool reasoning"
    assert len(requests) == 3
    for body in requests[1:]:
        assert body["tools"][0]["function"]["name"] == "lookup"
        assert "tool_choice" not in body
        assert body["model"] == model
    prior_assistants = [m for m in requests[-1]["messages"] if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in prior_assistants] == [
        "prior non-tool reasoning", "tool selection reasoning",
    ]
    assert requests[-1]["messages"][-1]["tool_call_id"] == "offline-call"
