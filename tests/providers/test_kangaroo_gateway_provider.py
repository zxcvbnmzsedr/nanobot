from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.config.schema import Config
from nanobot.identity.credentials import KangarooCredentialStore
from nanobot.identity.kangaroo import KangarooTokenBundle
from nanobot.identity.principal import IDENTITY_METADATA_KEY, Principal
from nanobot.providers.factory import make_provider, provider_signature
from nanobot.providers.kangaroo_gateway_provider import KangarooGatewayProvider


def _principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, org_id="org-1")


def _context(principal: Principal) -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id="chat-1",
        message_id="message-1",
        session_key="websocket:chat-1",
        original_user_text="查询 VIN",
        turn_id="websocket:chat-1:123456789",
        metadata={IDENTITY_METADATA_KEY: principal.metadata()},
    )


def _ndjson(*events: dict[str, Any]) -> bytes:
    return "".join(json.dumps(event) + "\n" for event in events).encode()


@pytest.mark.asyncio
async def test_stream_converts_content_reasoning_tools_and_usage() -> None:
    principal = _principal("user-1")
    store = KangarooCredentialStore()
    store.put(principal, "kangaroo-token")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=_ndjson(
                {"event": "response.started", "requestId": "req-1"},
                {"event": "reasoning.delta", "delta": "分析中"},
                {"event": "content.delta", "delta": "你好"},
                {
                    "event": "tool_call.delta",
                    "index": 0,
                    "id": "call-1",
                    "name": "search",
                    "argumentsDelta": '{"q":',
                },
                {
                    "event": "tool_call.completed",
                    "toolCall": {
                        "id": "call-1",
                        "name": "search",
                        "arguments": '{"q":"VIN"}',
                    },
                },
                {
                    "event": "usage",
                    "promptTokens": 12,
                    "completionTokens": 5,
                    "totalTokens": 17,
                },
                {"event": "response.completed", "finishReason": "tool_calls"},
            ),
            request=request,
        )

    provider = KangarooGatewayProvider(
        proxy_url="https://agent.example.com/nanobot/llm/stream",
        default_model="server-managed",
        credential_store=store,
        transport=httpx.MockTransport(handler),
    )
    content_deltas: list[str] = []
    thinking_deltas: list[str] = []
    tool_deltas: list[dict[str, Any]] = []

    async def on_content(delta: str) -> None:
        content_deltas.append(delta)

    async def on_thinking(delta: str) -> None:
        thinking_deltas.append(delta)

    async def on_tool(delta: dict[str, Any]) -> None:
        tool_deltas.append(delta)

    with request_context(_context(principal)):
        response = await provider.chat_stream(
            messages=[{"role": "user", "content": "查询 VIN", "internal": "drop-me"}],
            tools=[{"type": "function", "function": {"name": "search"}}],
            max_tokens=2048,
            reasoning_effort="medium",
            on_content_delta=on_content,
            on_thinking_delta=on_thinking,
            on_tool_call_delta=on_tool,
        )

    assert captured["authorization"] == "Bearer kangaroo-token"
    assert captured["body"]["conversation"] == {
        "conversationId": "chat-1",
        "turnId": "9e0d5b1972991e6363efae42c52a7cd976084d276c48a6d1cf67859ef94ce619",
        "messageId": "9deb880b43bdf6f465a0afb130aed71b31cf219626f3637f577d4167cd80e5f2",
        "channel": "websocket",
        "userMessage": "查询 VIN",
    }
    assert captured["body"]["messages"] == [{"role": "user", "content": "查询 VIN"}]
    assert captured["body"]["generation"] == {
        "maxTokens": 2048,
        "temperature": 0.7,
        "reasoningEffort": "medium",
    }
    assert response.content == "你好"
    assert response.reasoning_content == "分析中"
    assert response.finish_reason == "tool_calls"
    assert response.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }
    assert response.tool_calls[0].arguments == {"q": "VIN"}
    assert content_deltas == ["你好"]
    assert thinking_deltas == ["分析中"]
    assert tool_deltas == [{
        "index": 0,
        "call_id": "call-1",
        "name": "search",
        "arguments_delta": '{"q":',
    }]


@pytest.mark.asyncio
async def test_concurrent_users_never_share_authorization() -> None:
    first = _principal("user-1")
    second = _principal("user-2")
    store = KangarooCredentialStore()
    store.put(first, "token-one")
    store.put(second, "token-two")
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        await asyncio.sleep(0)
        seen[prompt] = request.headers["Authorization"]
        return httpx.Response(
            200,
            content=_ndjson(
                {"event": "content.delta", "delta": prompt},
                {"event": "response.completed", "finishReason": "stop"},
            ),
            request=request,
        )

    provider = KangarooGatewayProvider(
        proxy_url="https://agent.example.com/nanobot/llm/stream",
        default_model="server-managed",
        credential_store=store,
        transport=httpx.MockTransport(handler),
    )

    async def call(principal: Principal, prompt: str):
        with request_context(_context(principal)):
            return await provider.chat([{"role": "user", "content": prompt}])

    responses = await asyncio.gather(call(first, "first"), call(second, "second"))

    assert [response.content for response in responses] == ["first", "second"]
    assert seen == {
        "first": "Bearer token-one",
        "second": "Bearer token-two",
    }


@pytest.mark.asyncio
async def test_missing_identity_rejects_request_without_network_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    provider = KangarooGatewayProvider(
        proxy_url="https://agent.example.com/nanobot/llm/stream",
        default_model="server-managed",
        credential_store=KangarooCredentialStore(),
        transport=httpx.MockTransport(handler),
    )

    response = await provider.chat([{"role": "user", "content": "hello"}])

    assert response.finish_reason == "error"
    assert response.error_code == "KANGAROO_IDENTITY_REQUIRED"
    assert response.error_should_retry is False
    assert calls == 0


@pytest.mark.asyncio
async def test_unauthorized_response_refreshes_and_retries_once() -> None:
    principal = _principal("user-1")
    refresh_calls: list[str] = []

    async def refresh(refresh_token: str) -> KangarooTokenBundle:
        refresh_calls.append(refresh_token)
        return KangarooTokenBundle(
            access_token="refreshed-token",
            refresh_token="rotated-refresh",
            expires_at=9_999_999_999,
        )

    store = KangarooCredentialStore(refresher=refresh)
    store.put(principal, "old-token", refresh_token="old-refresh")
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["Authorization"]
        authorizations.append(authorization)
        if authorization == "Bearer old-token":
            return httpx.Response(401, request=request)
        return httpx.Response(
            200,
            content=_ndjson(
                {"event": "content.delta", "delta": "ok"},
                {"event": "response.completed", "finishReason": "stop"},
            ),
            request=request,
        )

    provider = KangarooGatewayProvider(
        proxy_url="https://agent.example.com/nanobot/llm/stream",
        default_model="server-managed",
        credential_store=store,
        transport=httpx.MockTransport(handler),
    )

    with request_context(_context(principal)):
        response = await provider.chat([{"role": "user", "content": "hello"}])

    assert response.content == "ok"
    assert authorizations == ["Bearer old-token", "Bearer refreshed-token"]
    assert refresh_calls == ["old-refresh"]
    assert store.get(principal.user_scope) == "refreshed-token"


@pytest.mark.asyncio
async def test_second_unauthorized_response_clears_refreshed_credential() -> None:
    principal = _principal("user-1")

    async def refresh(_: str) -> KangarooTokenBundle:
        return KangarooTokenBundle(
            access_token="refreshed-token",
            refresh_token="rotated-refresh",
            expires_at=9_999_999_999,
        )

    store = KangarooCredentialStore(refresher=refresh)
    store.put(principal, "old-token", refresh_token="old-refresh")
    provider = KangarooGatewayProvider(
        proxy_url="https://agent.example.com/nanobot/llm/stream",
        default_model="server-managed",
        credential_store=store,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, request=request)),
    )

    with request_context(_context(principal)):
        response = await provider.chat([{"role": "user", "content": "hello"}])

    assert response.error_code == "KANGAROO_CREDENTIAL_REJECTED"
    assert store.get(principal.user_scope) is None


def test_factory_forces_kangaroo_provider_without_api_key() -> None:
    config = Config.model_validate({
        "channels": {
            "websocket": {
                "enabled": True,
                "kangarooAuth": {
                    "enabled": True,
                    "apiBase": "https://accounts.example.com/",
                    "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
                },
            }
        }
    })

    provider = make_provider(config)

    assert isinstance(provider, KangarooGatewayProvider)
    assert provider.api_key is None
    assert provider_signature(config)[0] == "kangaroo_gateway"
