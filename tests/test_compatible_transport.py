"""Real SDK parsing of compatible-provider replies, without network requests."""

import httpx
import pytest

from bunnyland.llm_agents.agent import OpenRouterAgent
from bunnyland.llm_agents.compatible_transport import CompatibleTransport
from bunnyland.llm_agents.tools import ToolCall
from bunnyland.prompts.builder import PromptContext


@pytest.mark.parametrize("chat", [False, True])
async def test_qwen_reply_without_fingerprint_reaches_agent(monkeypatch, chat):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3.8-27b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop" if chat else "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "我是林冲。" if chat else None,
                            **(
                                {}
                                if chat
                                else {
                                    "tool_calls": [
                                        {
                                            "id": "call-test",
                                            "type": "function",
                                            "function": {
                                                "name": "say",
                                                "arguments": '{"text":"我是林冲。"}',
                                            },
                                        }
                                    ]
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(respond))
    agent = OpenRouterAgent(
        model="qwen3.8-27b", api_key="test-key", server_url="https://example.invalid/v1"
    )
    try:
        if chat:
            reply = await agent.chat([{"role": "user", "content": "你是谁？"}], character_id="lin")
            assert reply.content == "我是林冲。"
        else:
            context = PromptContext("林冲", "人", "awake", (5, 5), (5, 5), "空地", "树林")
            reply = await agent.decide("请回答。", context, character_id="lin")
            assert reply == ToolCall("say", {"text": "我是林冲。"})
        # Compatible providers have no OpenRouter /generation billing endpoint.
        assert len(requests) == 1
        assert requests[0].url.path == "/v1/chat/completions"
    finally:
        await agent.close()


@pytest.mark.parametrize(
    ("status", "path", "body"),
    [
        (401, "/chat/completions", b'{"error":"unauthorized"}'),
        (200, "/other", b"{}"),
        (200, "/chat/completions", b"not json"),
        (200, "/chat/completions", b"[]"),
        (200, "/chat/completions", b'{"system_fingerprint":"original"}'),
    ],
)
async def test_unrelated_or_invalid_responses_are_not_rewritten(status, path, body):
    transport = CompatibleTransport(
        httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("https://example.invalid" + path)
        assert response.status_code == status
        assert response.content == body
