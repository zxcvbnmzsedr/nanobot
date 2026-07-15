"""LLM provider backed by the authenticated Kangaroo model gateway."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.context import current_request_context
from nanobot.identity.credentials import (
    KangarooCredentialStore,
    get_kangaroo_credential_store,
)
from nanobot.identity.kangaroo import KangarooIdentityError
from nanobot.identity.principal import IDENTITY_METADATA_KEY
from nanobot.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    parse_tool_arguments,
    resolve_stream_idle_timeout_s,
)

_ALLOWED_MESSAGE_KEYS = frozenset({"role", "content", "name", "tool_calls", "tool_call_id"})
_MAX_EVENT_LINE_BYTES = 1_048_576


class KangarooGatewayProvider(LLMProvider):
    """Send each model request with the current user's Kangaroo access token."""

    supports_progress_deltas = True

    def __init__(
        self,
        *,
        proxy_url: str,
        default_model: str,
        credential_store: KangarooCredentialStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_s: float = 10.0,
    ) -> None:
        super().__init__(api_key=None, api_base=proxy_url)
        parsed = urlparse(proxy_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Kangaroo LLM proxy URL must be an absolute HTTP(S) URL")
        self.proxy_url = proxy_url
        self.default_model = default_model
        self._credential_store = credential_store or get_kangaroo_credential_store()
        self._transport = transport
        self._connect_timeout_s = connect_timeout_s

    def get_default_model(self) -> str:
        return self.default_model

    @staticmethod
    def _request_identity() -> tuple[str, dict[str, Any]] | None:
        context = current_request_context()
        if context is None:
            return None
        identity = context.metadata.get(IDENTITY_METADATA_KEY)
        if not isinstance(identity, dict) or identity.get("source") != "kangaroo":
            return None
        user_scope = identity.get("user_scope")
        if not isinstance(user_scope, str) or not user_scope:
            return None
        return user_scope, identity

    @staticmethod
    def _error(
        message: str,
        *,
        status_code: int | None = None,
        kind: str | None = None,
        code: str | None = None,
        should_retry: bool | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content=f"Error calling LLM: {message}",
            finish_reason="error",
            error_status_code=status_code,
            error_kind=kind,
            error_code=code,
            error_should_retry=should_retry,
        )

    @staticmethod
    def _reasoning_effort(value: str | None) -> str | None:
        if value in {"none", "low", "medium", "high"}:
            return value
        if value in {"minimal", "minimum"}:
            return "low"
        if value == "adaptive":
            return "high"
        return None

    @classmethod
    def _payload(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        sanitized = cls._sanitize_request_messages(
            cls._sanitize_empty_content(messages),
            _ALLOWED_MESSAGE_KEYS,
        )
        generation: dict[str, Any] = {
            "maxTokens": max(1, min(int(max_tokens), 131_072)),
            "temperature": max(0.0, min(float(temperature), 2.0)),
        }
        normalized_effort = cls._reasoning_effort(reasoning_effort)
        if normalized_effort is not None:
            generation["reasoningEffort"] = normalized_effort
        payload: dict[str, Any] = {
            "messages": sanitized,
            "tools": tools or [],
            "generation": generation,
        }
        if tool_choice is not None:
            payload["toolChoice"] = tool_choice
        return payload

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self.chat_stream(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        del model  # The server owns model selection.
        request_identity = self._request_identity()
        if request_identity is None:
            return self._error(
                "当前请求缺少已验证的袋鼠用户身份。",
                status_code=401,
                code="KANGAROO_IDENTITY_REQUIRED",
                should_retry=False,
            )
        user_scope, _identity = request_identity
        try:
            access_token = await self._credential_store.get_valid_access_token(user_scope)
        except KangarooIdentityError as exc:
            return self._error(
                "袋鼠登录凭证刷新失败，请稍后重试。",
                status_code=exc.http_status,
                code="KANGAROO_CREDENTIAL_REFRESH_FAILED",
                should_retry=exc.http_status >= 500,
            )
        if access_token is None:
            return self._error(
                "袋鼠登录凭证不可用，请重新登录。",
                status_code=401,
                code="KANGAROO_CREDENTIAL_REQUIRED",
                should_retry=False,
            )

        payload = self._payload(
            messages,
            tools,
            max_tokens,
            temperature,
            reasoning_effort,
            tool_choice,
        )
        timeout = httpx.Timeout(
            connect=self._connect_timeout_s,
            read=resolve_stream_idle_timeout_s(),
            write=30.0,
            pool=30.0,
        )
        for attempt in range(2):
            result = await self._request_once(
                access_token=access_token,
                payload=payload,
                timeout=timeout,
                on_content_delta=on_content_delta,
                on_thinking_delta=on_thinking_delta,
                on_tool_call_delta=on_tool_call_delta,
            )
            if result is not None:
                return result
            if attempt > 0:
                break
            try:
                refreshed = await self._credential_store.refresh_access_token(
                    user_scope,
                    rejected_access_token=access_token,
                )
            except KangarooIdentityError as exc:
                return self._error(
                    "袋鼠登录凭证刷新失败，请稍后重试。",
                    status_code=exc.http_status,
                    code="KANGAROO_CREDENTIAL_REFRESH_FAILED",
                    should_retry=exc.http_status >= 500,
                )
            if refreshed is None:
                break
            access_token = refreshed

        self._credential_store.remove(user_scope, access_token=access_token)
        return self._error(
            "袋鼠登录已失效，请重新登录。",
            status_code=401,
            code="KANGAROO_CREDENTIAL_REJECTED",
            should_retry=False,
        )

    async def _request_once(
        self,
        *,
        access_token: str,
        payload: dict[str, Any],
        timeout: httpx.Timeout,
        on_content_delta: Callable[[str], Awaitable[None]] | None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> LLMResponse | None:
        """Send one proxy request; ``None`` means the credential was rejected."""
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self.proxy_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/x-ndjson",
                        "Content-Type": "application/json",
                        "X-Request-Id": secrets.token_hex(16),
                    },
                    json=payload,
                ) as response:
                    if response.status_code == 401:
                        return None
                    if response.status_code != 200:
                        return self._error(
                            "袋鼠模型代理暂时不可用。",
                            status_code=response.status_code,
                            code="KANGAROO_PROXY_HTTP_ERROR",
                            should_retry=response.status_code >= 500 or response.status_code == 429,
                        )
                    return await self._consume_stream(
                        response,
                        on_content_delta=on_content_delta,
                        on_thinking_delta=on_thinking_delta,
                        on_tool_call_delta=on_tool_call_delta,
                    )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            return self._error(
                "袋鼠模型代理响应超时。",
                kind="timeout",
                code="KANGAROO_PROXY_TIMEOUT",
                should_retry=True,
            )
        except httpx.HTTPError:
            return self._error(
                "无法连接袋鼠模型代理。",
                kind="connection",
                code="KANGAROO_PROXY_UNAVAILABLE",
                should_retry=True,
            )

    async def _consume_stream(
        self,
        response: httpx.Response,
        *,
        on_content_delta: Callable[[str], Awaitable[None]] | None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        usage: dict[str, int] = {}
        finish_reason = "stop"
        completed = False

        async for line in response.aiter_lines():
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > _MAX_EVENT_LINE_BYTES:
                return self._error(
                    "袋鼠模型代理返回了过大的事件。",
                    code="KANGAROO_PROXY_INVALID_STREAM",
                    should_retry=False,
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return self._error(
                    "袋鼠模型代理返回了无效数据。",
                    code="KANGAROO_PROXY_INVALID_STREAM",
                    should_retry=False,
                )
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                return self._error(
                    "袋鼠模型代理返回了无效事件。",
                    code="KANGAROO_PROXY_INVALID_STREAM",
                    should_retry=False,
                )

            event_type = event["event"]
            if event_type == "content.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    content_parts.append(delta)
                    if on_content_delta is not None:
                        await on_content_delta(delta)
            elif event_type == "reasoning.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    reasoning_parts.append(delta)
                    if on_thinking_delta is not None:
                        await on_thinking_delta(delta)
            elif event_type == "tool_call.delta" and on_tool_call_delta is not None:
                index = event.get("index")
                await on_tool_call_delta({
                    "index": index if isinstance(index, int) else 0,
                    "call_id": str(event.get("id") or ""),
                    "name": str(event.get("name") or ""),
                    "arguments_delta": str(event.get("argumentsDelta") or ""),
                })
            elif event_type == "tool_call.completed":
                raw_call = event.get("toolCall")
                if isinstance(raw_call, dict):
                    tool_calls.append(ToolCallRequest(
                        id=str(raw_call.get("id") or ""),
                        name=str(raw_call.get("name") or ""),
                        arguments=parse_tool_arguments(raw_call.get("arguments")),
                    ))
            elif event_type == "usage":
                usage = {
                    "prompt_tokens": int(event.get("promptTokens") or 0),
                    "completion_tokens": int(event.get("completionTokens") or 0),
                    "total_tokens": int(event.get("totalTokens") or 0),
                }
            elif event_type == "response.error":
                code = str(event.get("code") or "KANGAROO_PROXY_ERROR")
                return self._error(
                    str(event.get("message") or "袋鼠模型代理处理失败。"),
                    kind="timeout" if code == "UPSTREAM_TIMEOUT" else None,
                    code=code,
                    should_retry=code in {
                        "UPSTREAM_TIMEOUT",
                        "UPSTREAM_UNAVAILABLE",
                        "UPSTREAM_HTTP_ERROR",
                    },
                )
            elif event_type == "response.completed":
                finish_reason = str(event.get("finishReason") or "stop")
                completed = True

        if not completed:
            return self._error(
                "袋鼠模型代理的响应意外中断。",
                kind="connection",
                code="KANGAROO_PROXY_INCOMPLETE_STREAM",
                should_retry=True,
            )
        return LLMResponse(
            content="".join(content_parts) or None,
            reasoning_content="".join(reasoning_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
