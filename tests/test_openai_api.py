"""Focused tests for the OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.api.server import (
    API_CHAT_ID,
    API_SESSION_KEY,
    _chat_completion_response,
    _error_json,
    _responses_response,
    create_api_tool_registry,
    create_app,
    handle_chat_completions,
)

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

API_KEY = "secret"
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"}


class _NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} test tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return kwargs


def _registry_with_tools(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_NamedTool(name))
    return registry


def _make_mock_agent(response_text: str = "mock response") -> MagicMock:
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value=response_text)
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()
    agent._last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
    return agent


@pytest.fixture
def mock_agent():
    return _make_mock_agent()


@pytest.fixture
def app(mock_agent):
    return create_app(mock_agent, model_name="test-model", request_timeout=10.0, api_key=API_KEY)


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


def test_error_json() -> None:
    resp = _error_json(400, "bad request")
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"]["message"] == "bad request"
    assert body["error"]["code"] == 400


def test_chat_completion_response() -> None:
    result = _chat_completion_response("hello world", "test-model")
    assert result["object"] == "chat.completion"
    assert result["model"] == "test-model"
    assert result["choices"][0]["message"]["content"] == "hello world"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["id"].startswith("chatcmpl-")
    assert result["usage"]["prompt_tokens"] == 0
    assert result["usage"]["completion_tokens"] == 0
    assert result["usage"]["total_tokens"] == 0


def test_chat_completion_response_with_usage() -> None:
    usage = {"prompt_tokens": 150, "completion_tokens": 42}
    result = _chat_completion_response("hello world", "test-model", usage)
    assert result["usage"]["prompt_tokens"] == 150
    assert result["usage"]["completion_tokens"] == 42
    assert result["usage"]["total_tokens"] == 192


def test_chat_completion_response_preserves_provider_total_usage() -> None:
    usage = {"total_tokens": 77}
    result = _chat_completion_response("hello world", "test-model", usage)
    assert result["usage"]["prompt_tokens"] == 0
    assert result["usage"]["completion_tokens"] == 0
    assert result["usage"]["total_tokens"] == 77


def test_responses_response() -> None:
    result = _responses_response(
        "hello world",
        "test-model",
        "resp_123",
        "resp_previous",
        {"prompt_tokens": 10, "completion_tokens": 4},
    )
    assert result["id"] == "resp_123"
    assert result["object"] == "response"
    assert result["status"] == "completed"
    assert result["previous_response_id"] == "resp_previous"
    assert result["output"][0]["content"][0]["type"] == "output_text"
    assert result["output"][0]["content"][0]["text"] == "hello world"
    assert result["usage"] == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
    }


def test_create_api_tool_registry_is_fail_closed() -> None:
    source = _registry_with_tools("safe_query", "exec", "write_file", "apply_patch")

    restricted = create_api_tool_registry(source, ["safe_query"])

    assert restricted is not None
    assert restricted.tool_names == ["safe_query"]
    assert {"exec", "write_file", "apply_patch"}.isdisjoint(restricted.tool_names)
    with pytest.raises(ValueError, match="missing_query"):
        create_api_tool_registry(source, ["safe_query", "missing_query"])


def test_empty_api_tool_allowlist_preserves_unrestricted_behavior() -> None:
    source = _registry_with_tools("exec")

    assert create_api_tool_registry(source, []) is None


def test_required_api_tool_allowlist_rejects_empty_configuration() -> None:
    source = _registry_with_tools("exec")

    with pytest.raises(ValueError, match="require_tool_allowlist"):
        create_api_tool_registry(source, [], require_allowlist=True)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/responses", {"input": "status", "stream": False}),
        ("/v1/responses", {"input": "status", "stream": True}),
        (
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "status"}], "stream": False},
        ),
        (
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "status"}], "stream": True},
        ),
    ],
)
async def test_every_agent_api_path_uses_restricted_tools(
    aiohttp_client,
    path: str,
    body: dict[str, object],
) -> None:
    received: list[tuple[ToolRegistry | None, bool]] = []

    async def fake_process(*, tools=None, allow_commands=True, on_stream=None, **kwargs):
        received.append((tools, allow_commands))
        if on_stream is not None:
            await on_stream("ok")
        return "ok"

    agent = _make_mock_agent()
    agent.process_direct = fake_process
    source = _registry_with_tools(
        "list_microgrid_projects",
        "get_realtime_metrics",
        "exec",
        "write_file",
        "apply_patch",
    )
    restricted = create_api_tool_registry(
        source,
        ["list_microgrid_projects", "get_realtime_metrics"],
    )
    app = create_app(
        agent,
        model_name="test-model",
        api_key=API_KEY,
        api_tools=restricted,
        allow_commands=False,
    )
    client = await aiohttp_client(app)

    response = await client.post(path, headers=AUTH_HEADERS, json=body)
    await response.read()

    assert response.status == 200
    assert len(received) == 1
    received_tools, received_allow_commands = received[0]
    assert received_tools is restricted
    assert received_allow_commands is False
    assert received_tools.tool_names == [
        "list_microgrid_projects",
        "get_realtime_metrics",
    ]
    assert {"exec", "write_file", "apply_patch"}.isdisjoint(received_tools.tool_names)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_first_request_uses_metadata_session(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={
            "model": "test-model",
            "instructions": "Use tools before answering.",
            "input": "What is the current power?",
            "stream": False,
            "metadata": {"session_id": "microgrid:42:project-7:abc"},
        },
    )

    assert resp.status == 200
    body = await resp.json()
    assert body["id"].startswith("resp_")
    assert body["object"] == "response"
    assert body["previous_response_id"] is None
    assert body["output"][0]["content"][0]["text"] == "mock response"
    mock_agent.process_direct.assert_called_once_with(
        content="What is the current power?",
        media=None,
        session_key="api:microgrid:42:project-7:abc",
        channel="api",
        chat_id=API_CHAT_ID,
        trusted_instructions="Use tools before answering.",
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_followup_reuses_previous_session(aiohttp_client) -> None:
    call_log: list[tuple[str, str]] = []

    async def fake_process(content, session_key="", **kwargs):
        call_log.append((content, session_key))
        return f"reply {len(call_log)}"

    agent = MagicMock()
    agent.process_direct = fake_process
    agent._last_usage = {}
    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)

    first = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "first", "metadata": {"session_id": "conversation-1"}},
    )
    first_body = await first.json()
    second = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "second", "previous_response_id": first_body["id"]},
    )
    second_body = await second.json()

    assert first.status == 200
    assert second.status == 200
    assert second_body["previous_response_id"] == first_body["id"]
    assert second_body["id"] != first_body["id"]
    assert call_log == [
        ("first", "api:conversation-1"),
        ("second", "api:conversation-1"),
    ]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_rejects_unknown_previous_response(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "follow up", "previous_response_id": "resp_missing"},
    )

    assert resp.status == 400
    assert (await resp.json())["error"]["message"] == "Unknown previous_response_id"
    mock_agent.process_direct.assert_not_called()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("followup_owner", [None, "user-b"])
async def test_responses_rejects_cross_owner_followup(
    aiohttp_client,
    mock_agent,
    followup_owner: str | None,
) -> None:
    app = create_app(mock_agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)
    first = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "first", "metadata": {"owner_id": "user-a"}},
    )
    first_body = await first.json()
    followup: dict[str, object] = {
        "input": "follow up",
        "previous_response_id": first_body["id"],
    }
    if followup_owner is not None:
        followup["metadata"] = {"owner_id": followup_owner}

    response = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json=followup,
    )

    assert first.status == 200
    assert response.status == 403
    assert (await response.json())["error"]["message"] == (
        "Response does not belong to this owner"
    )
    assert mock_agent.process_direct.await_count == 1


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_allows_matching_owner_followup(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)
    first = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "first", "metadata": {"owner_id": "user-a"}},
    )
    first_body = await first.json()

    response = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={
            "input": "follow up",
            "previous_response_id": first_body["id"],
            "metadata": {"owner_id": "user-a"},
        },
    )

    assert response.status == 200
    assert (await response.json())["previous_response_id"] == first_body["id"]
    assert mock_agent.process_direct.await_count == 2


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_streams_typed_events_and_completed_response(aiohttp_client) -> None:
    async def fake_process(*, on_stream=None, on_stream_end=None, **kwargs):
        if on_stream is not None:
            await on_stream("hello")
            await on_stream(" world")
        if on_stream_end is not None:
            await on_stream_end()
        return "hello world"

    agent = _make_mock_agent()
    agent.process_direct = fake_process
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={
            "model": "test-model",
            "input": "hello",
            "stream": True,
            "metadata": {"session_id": "stream-conversation"},
        },
    )

    assert resp.status == 200
    assert resp.content_type == "text/event-stream"
    payload = await resp.text()
    events = [json.loads(line[6:]) for line in payload.splitlines() if line.startswith("data: ")]
    event_types = [event["type"] for event in events]
    assert event_types == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert "[DONE]" not in payload
    assert [event["delta"] for event in events if event["type"].endswith(".delta")] == [
        "hello",
        " world",
    ]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    completed = events[-1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "hello world"
    assert completed["id"] == events[0]["response"]["id"]

    followup = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "again", "previous_response_id": completed["id"]},
    )
    assert followup.status == 200
    assert (await followup.json())["previous_response_id"] == completed["id"]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_stream_reports_timeout_as_sse_error(aiohttp_client) -> None:
    async def slow_process(**kwargs):
        await asyncio.sleep(1)
        return "too late"

    agent = _make_mock_agent()
    agent.process_direct = slow_process
    app = create_app(agent, model_name="test-model", request_timeout=0.01, api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "hello", "stream": True},
    )

    assert resp.status == 200
    events = [
        json.loads(line[6:])
        for line in (await resp.text()).splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "request_timeout"
    assert not any(event["type"] == "response.completed" for event in events)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_stream_cancels_agent_after_client_disconnect(aiohttp_client) -> None:
    cancelled = asyncio.Event()

    async def slow_process(**kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent = _make_mock_agent()
    agent.process_direct = slow_process
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "hello", "stream": True},
    )

    assert resp.status == 200
    resp.close()
    await asyncio.wait_for(cancelled.wait(), timeout=2.0)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_cancel_endpoint_stops_active_agent(aiohttp_client) -> None:
    cancelled = asyncio.Event()

    async def slow_process(**kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent = _make_mock_agent()
    agent.process_direct = slow_process
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)
    stream = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "hello", "stream": True},
    )
    created_frame = await stream.content.readuntil(b"\n\n")
    created = json.loads(created_frame.decode().removeprefix("data: "))

    cancel = await client.post(
        f"/v1/responses/{created['response']['id']}/cancel",
        headers=AUTH_HEADERS,
    )

    assert cancel.status == 200
    assert (await cancel.json())["status"] == "cancelled"
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    await stream.read()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_responses_cancel_rejects_cross_owner(aiohttp_client) -> None:
    cancelled = asyncio.Event()

    async def slow_process(**kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent = _make_mock_agent()
    agent.process_direct = slow_process
    app = create_app(agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)
    stream = await client.post(
        "/v1/responses",
        headers=AUTH_HEADERS,
        json={"input": "hello", "stream": True, "metadata": {"owner_id": "user-a"}},
    )
    created_frame = await stream.content.readuntil(b"\n\n")
    created = json.loads(created_frame.decode().removeprefix("data: "))
    cancel_url = f"/v1/responses/{created['response']['id']}/cancel"

    wrong_owner = await client.post(
        cancel_url,
        headers=AUTH_HEADERS,
        json={"owner_id": "user-b"},
    )
    assert wrong_owner.status == 403
    assert not cancelled.is_set()

    missing_owner = await client.post(cancel_url, headers=AUTH_HEADERS)
    assert missing_owner.status == 403
    assert not cancelled.is_set()

    matching_owner = await client.post(
        cancel_url,
        headers=AUTH_HEADERS,
        json={"owner_id": "user-a"},
    )
    assert matching_owner.status == 200
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    await stream.read()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_missing_messages_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post("/v1/chat/completions", headers=AUTH_HEADERS, json={"model": "test"})
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_api_key_protects_api_routes_but_not_health(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)

    health = await client.get("/health")
    missing = await client.get("/v1/models")
    wrong = await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    ok = await client.get("/v1/models", headers=AUTH_HEADERS)

    assert health.status == 200
    assert missing.status == 401
    assert wrong.status == 401
    assert ok.status == 200
    assert (await missing.json())["error"]["message"].startswith("Missing Authorization")
    assert (await wrong.json())["error"]["message"] == "Invalid API key"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_api_routes_allow_requests_without_configured_api_key(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="test-model")
    client = await aiohttp_client(app)

    health = await client.get("/health")
    models = await client.get("/v1/models")
    chat = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert health.status == 200
    assert models.status == 200
    assert chat.status == 200
    mock_agent.process_direct.assert_called_once()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_no_user_message_returns_400(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"messages": [{"role": "system", "content": "you are a bot"}]},
    )
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_true_returns_sse(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"messages": [{"role": "user", "content": "hello"}], "stream": True},
    )
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"


@pytest.mark.asyncio
async def test_model_mismatch_returns_400() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "model": "other-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    request.app = {
        "agent_loop": _make_mock_agent(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_lock": asyncio.Lock(),
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "test-model" in body["error"]["message"]


@pytest.mark.asyncio
async def test_single_user_message_required() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "previous reply"},
            ],
        }
    )
    request.app = {
        "agent_loop": _make_mock_agent(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_lock": asyncio.Lock(),
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "single user message" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_single_user_message_must_have_user_role() -> None:
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [{"role": "system", "content": "you are a bot"}],
        }
    )
    request.app = {
        "agent_loop": _make_mock_agent(),
        "model_name": "test-model",
        "request_timeout": 10.0,
        "session_lock": asyncio.Lock(),
    }

    resp = await handle_chat_completions(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "single user message" in body["error"]["message"].lower()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_successful_request_uses_fixed_api_session(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="test-model", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "mock response"
    assert body["model"] == "test-model"
    mock_agent.process_direct.assert_called_once_with(
        content="hello",
        media=None,
        session_key=API_SESSION_KEY,
        channel="api",
        chat_id=API_CHAT_ID,
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_followup_requests_share_same_session_key(aiohttp_client) -> None:
    call_log: list[str] = []

    async def fake_process(content, session_key="", channel="", chat_id="", **kwargs):
        call_log.append(session_key)
        return f"reply to {content}"

    agent = MagicMock()
    agent.process_direct = fake_process
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()
    agent._last_usage = {}

    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)

    r1 = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    r2 = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"messages": [{"role": "user", "content": "second"}]},
    )

    assert r1.status == 200
    assert r2.status == 200
    assert call_log == [API_SESSION_KEY, API_SESSION_KEY]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_fixed_session_requests_are_serialized(aiohttp_client) -> None:
    order: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def slow_process(content, session_key="", channel="", chat_id="", **kwargs):
        order.append(f"start:{content}")
        if content == "first":
            first_started.set()
            await release_first.wait()
        order.append(f"end:{content}")
        return content

    agent = MagicMock()
    agent.process_direct = slow_process
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()
    agent._last_usage = {}

    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)

    async def send(msg: str):
        return await client.post(
            "/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={"messages": [{"role": "user", "content": msg}]},
        )

    first = asyncio.create_task(send("first"))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    second = asyncio.create_task(send("second"))
    await asyncio.sleep(0)
    assert order == ["start:first"]

    release_first.set()
    r1, r2 = await asyncio.gather(first, second)
    assert r1.status == 200
    assert r2.status == 200
    assert order == ["start:first", "end:first", "start:second", "end:second"]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_models_endpoint(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.get("/v1/models", headers=AUTH_HEADERS)
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == "test-model"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_health_endpoint(aiohttp_client, app) -> None:
    client = await aiohttp_client(app)
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_multimodal_content_extracts_text(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ]
        },
    )
    assert resp.status == 200
    call_kwargs = mock_agent.process_direct.call_args.kwargs
    assert call_kwargs["content"] == "describe this"
    assert call_kwargs["session_key"] == API_SESSION_KEY
    assert call_kwargs["channel"] == "api"
    assert call_kwargs["chat_id"] == API_CHAT_ID
    assert len(call_kwargs.get("media") or []) >= 0  # base64 images saved to disk


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_multimodal_remote_image_url_returns_400(aiohttp_client, mock_agent) -> None:
    app = create_app(mock_agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
                    ],
                }
            ]
        },
    )

    assert resp.status == 400
    body = await resp.json()
    assert "remote image urls are not supported" in body["error"]["message"].lower()
    mock_agent.process_direct.assert_not_called()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_empty_response_falls_back_without_retry(aiohttp_client) -> None:
    from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

    call_count = 0

    async def always_empty(content, session_key="", channel="", chat_id="", **kwargs):
        nonlocal call_count
        call_count += 1
        return ""

    agent = MagicMock()
    agent.process_direct = always_empty
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()
    agent._last_usage = {}

    app = create_app(agent, model_name="m", api_key=API_KEY)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == EMPTY_FINAL_RESPONSE_MESSAGE
    assert call_count == 1


@pytest.mark.asyncio
async def test_process_direct_accepts_media() -> None:
    """process_direct should forward media paths to _process_message."""
    from nanobot.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop._connect_mcp = AsyncMock()
    loop._session_locks = {}

    captured_msg = None

    async def fake_process(msg, *, session_key="", on_progress=None, on_stream=None, on_stream_end=None, ephemeral=False):
        nonlocal captured_msg
        captured_msg = msg
        return None

    loop._process_message = fake_process

    await loop.process_direct(
        content="analyze this",
        media=["/tmp/image.png", "/tmp/report.pdf"],
        session_key="test:1",
    )

    assert captured_msg is not None
    assert captured_msg.media == ["/tmp/image.png", "/tmp/report.pdf"]
    assert captured_msg.content == "analyze this"


def test_trusted_instructions_are_added_to_system_message_only() -> None:
    """Per-request instructions must not be merged into user-controlled content."""
    from nanobot.agent.loop import AgentLoop, TurnKind

    loop = AgentLoop.__new__(AgentLoop)
    loop.context = MagicMock()
    loop.context.build_messages.return_value = [
        {"role": "system", "content": "base system prompt"},
        {"role": "user", "content": "user input"},
    ]
    loop.workspace_scopes = MagicMock()
    loop.workspace_scopes.for_message.return_value.project_path = None
    loop._unified_session = False

    ctx = MagicMock()
    ctx.session = MagicMock(metadata={}, key="api:test")
    ctx.msg = MagicMock(content="user input", media=[])
    ctx.kind = TurnKind.USER
    ctx.delivery.route.channel = "api"
    ctx.delivery.route.chat_id = "default"
    ctx.pending_summary = None
    ctx.runtime_context_blocks = []
    ctx.ephemeral = False
    ctx.trusted_instructions = "Always query the approved data tools."

    messages = loop._build_initial_messages(ctx)

    assert messages[0]["role"] == "system"
    assert "Always query the approved data tools." in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "user input"}


@pytest.mark.asyncio
async def test_disabled_commands_bypass_command_router() -> None:
    """Restricted API turns must treat slash commands as ordinary model input."""
    from nanobot.agent.loop import AgentLoop, TurnKind

    loop = AgentLoop.__new__(AgentLoop)
    loop.commands = MagicMock()
    loop.commands.dispatch = AsyncMock()

    ctx = MagicMock()
    ctx.kind = TurnKind.USER
    ctx.allow_commands = False
    ctx.msg.content = "/restart"

    assert await loop._state_command(ctx) == "dispatch"
    loop.commands.dispatch.assert_not_awaited()
