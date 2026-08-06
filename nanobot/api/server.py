"""OpenAI-compatible HTTP API server for nanobot.

Provides /v1/chat/completions, /v1/responses, and /v1/models endpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json as _json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.paths import get_media_dir
from nanobot.utils.helpers import safe_filename
from nanobot.utils.media_decode import (
    MAX_FILE_SIZE,
)
from nanobot.utils.media_decode import (
    FileSizeExceeded as _FileSizeExceeded,
)
from nanobot.utils.media_decode import (
    save_base64_data_url as _save_base64_data_url,
)
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

__all__ = (
    "MAX_FILE_SIZE",
    "_FileSizeExceeded",
    "_save_base64_data_url",
    "create_api_tool_registry",
    "create_app",
    "handle_chat_completions",
    "handle_cancel_response",
    "handle_responses",
)


API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"
_AGENT_LOOP_KEY = web.AppKey[Any]("agent_loop")
_MODEL_NAME_KEY = web.AppKey[str]("model_name")
_REQUEST_TIMEOUT_KEY = web.AppKey[float]("request_timeout")
_API_TOOLS_KEY = web.AppKey[ToolRegistry]("api_tools")
_API_ALLOW_COMMANDS_KEY = web.AppKey[bool]("api_allow_commands")
_SESSION_LOCKS_KEY = web.AppKey[dict]("session_locks")
_RESPONSE_SESSIONS_KEY = web.AppKey[OrderedDict]("response_sessions")
_ACTIVE_RESPONSE_TASKS_KEY = web.AppKey[dict]("active_response_tasks")
_MAX_RESPONSE_SESSIONS = 2048
_STREAM_HEARTBEAT_SECONDS = 1.0
_MISSING = object()


@dataclass(frozen=True, slots=True)
class _ResponseSession:
    session_key: str
    owner_id: str | None


@dataclass(frozen=True, slots=True)
class _ActiveResponse:
    task: asyncio.Task[Any]
    owner_id: str | None


def _app_value(
    app: Any,
    key: web.AppKey[Any],
    legacy_key: str,
    default: Any = _MISSING,
) -> Any:
    """Read typed aiohttp state while accepting lightweight dict test doubles."""
    try:
        return app[key]
    except KeyError:
        if default is _MISSING:
            return app[legacy_key]
        return app.get(legacy_key, default)


def _api_tool_kwargs(app: Any) -> dict[str, ToolRegistry]:
    tools = _app_value(app, _API_TOOLS_KEY, "api_tools", None)
    return {"tools": tools} if tools is not None else {}


def _api_command_kwargs(app: Any) -> dict[str, bool]:
    allow_commands = _app_value(
        app,
        _API_ALLOW_COMMANDS_KEY,
        "api_allow_commands",
        True,
    )
    return {} if allow_commands else {"allow_commands": False}


def _trusted_instruction_kwargs(instructions: str | None) -> dict[str, str]:
    return {"trusted_instructions": instructions} if instructions is not None else {}


def create_api_tool_registry(
    source: ToolRegistry,
    allowlist: list[str],
    *,
    require_allowlist: bool = False,
) -> ToolRegistry | None:
    """Create a fail-closed tool registry for API requests.

    An empty allowlist retains the existing unrestricted API behavior. Once an
    allowlist is configured, every named tool must exist at startup.
    """
    if not allowlist and require_allowlist:
        raise ValueError(
            "api.tool_allowlist must not be empty when "
            "api.require_tool_allowlist is enabled"
        )
    if not allowlist:
        return None

    restricted = ToolRegistry()
    missing: list[str] = []
    for name in allowlist:
        tool = source.get(name)
        if tool is None:
            missing.append(name)
        else:
            restricted.register(tool)

    if missing:
        raise ValueError(
            "api.tool_allowlist references unavailable tools: " + ", ".join(missing)
        )
    return restricted


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(
    content: str,
    model: str,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    prompt = (usage or {}).get("prompt_tokens", 0)
    completion = (usage or {}).get("completion_tokens", 0)
    total = (usage or {}).get("total_tokens", 0) or prompt + completion
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
    }


def _responses_response(
    content: str,
    model: str,
    response_id: str,
    previous_response_id: str | None = None,
    usage: dict[str, int] | None = None,
    *,
    message_id: str | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    prompt = (usage or {}).get("prompt_tokens", 0)
    completion = (usage or {}).get("completion_tokens", 0)
    total = (usage or {}).get("total_tokens", 0) or prompt + completion
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": model,
        "previous_response_id": previous_response_id,
        "output": [
            {
                "id": message_id or f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": total,
        },
    }


def _responses_sse_event(event: dict[str, Any]) -> bytes:
    """Encode one Responses API event as an SSE data frame."""
    payload = _json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n".encode()


def _response_text(value: Any) -> str:
    """Normalize process_direct output to plain assistant text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


def _parse_responses_input(
    body: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Return content, instructions, previous response, session id, and owner id."""
    input_value = body.get("input")
    if isinstance(input_value, str):
        text = input_value
    elif isinstance(input_value, list) and len(input_value) == 1:
        item = input_value[0]
        if not isinstance(item, dict) or item.get("role") != "user":
            raise ValueError("Only a single user input message is supported")
        content = item.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"input_text", "text"}
            ]
            text = " ".join(text_parts)
        else:
            raise ValueError("Invalid input content format")
    else:
        raise ValueError("Input must be a string or a single user message")

    if not text.strip():
        raise ValueError("Input must not be empty")

    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ValueError("Instructions must be a string")
    trusted_instructions = instructions.strip() if instructions else None

    previous_response_id = body.get("previous_response_id")
    if previous_response_id is not None and not isinstance(previous_response_id, str):
        raise ValueError("previous_response_id must be a string")

    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Metadata must be an object")
    initial_session_id = (metadata or {}).get("session_id")
    if initial_session_id is not None and not isinstance(initial_session_id, str):
        raise ValueError("metadata.session_id must be a string")
    owner_id = (metadata or {}).get("owner_id")
    if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
        raise ValueError("metadata.owner_id must be a non-empty string")

    if previous_response_id and initial_session_id:
        raise ValueError("metadata.session_id cannot be used with previous_response_id")
    return text, trusted_instructions, previous_response_id, initial_session_id, owner_id

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_chunk(delta: str, model: str, chunk_id: str, finish_reason: str | None = None) -> bytes:
    """Format a single OpenAI-compatible SSE chunk."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {_json.dumps(payload)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _parse_json_content(body: dict) -> tuple[str, list[str]]:
    """Parse JSON request body. Returns (text, media_paths)."""
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Only a single user message is supported")
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("Only a single user message is supported")

    user_content = message.get("content", "")
    media_dir = get_media_dir("api")
    media_paths: list[str] = []

    if isinstance(user_content, list):
        text_parts: list[str] = []
        for part in user_content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    saved = _save_base64_data_url(url, media_dir)
                    if saved:
                        media_paths.append(saved)
                elif url:
                    raise ValueError(
                        "Remote image URLs are not supported. "
                        "Use base64 data URLs or upload files via multipart/form-data."
                    )
        text = " ".join(text_parts)
    elif isinstance(user_content, str):
        text = user_content
    else:
        raise ValueError("Invalid content format")

    return text, media_paths


async def _parse_multipart(request: web.Request) -> tuple[str, list[str], str | None, str | None]:
    """Parse multipart/form-data. Returns (text, media_paths, session_id, model)."""
    media_dir = get_media_dir("api")
    reader = await request.multipart()
    text = ""
    session_id = None
    model = None
    media_paths: list[str] = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "model":
            model = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            raw = await part.read()
            if len(raw) > MAX_FILE_SIZE:
                raise _FileSizeExceeded(
                    f"File '{part.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                )
            base = safe_filename(part.filename or "upload.bin")
            filename = f"{uuid.uuid4().hex[:12]}_{base}"
            dest = media_dir / filename
            dest.write_bytes(raw)
            media_paths.append(str(dest))

    if not text:
        text = "请分析上传的文件"

    return text, media_paths, session_id, model


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def handle_chat_completions(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — supports JSON and multipart/form-data."""
    content_type = request.content_type or ""
    if not isinstance(content_type, str):
        content_type = ""

    agent_loop = _app_value(request.app, _AGENT_LOOP_KEY, "agent_loop")
    timeout_s: float = _app_value(
        request.app,
        _REQUEST_TIMEOUT_KEY,
        "request_timeout",
        120.0,
    )
    model_name: str = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "nanobot")

    stream = False
    try:
        if content_type.startswith("multipart/"):
            text, media_paths, session_id, requested_model = await _parse_multipart(request)
        else:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            stream = body.get("stream", False)
            requested_model = body.get("model")
            text, media_paths = _parse_json_content(body)
            session_id = body.get("session_id")
    except ValueError as e:
        return _error_json(400, str(e))
    except _FileSizeExceeded as e:
        return _error_json(413, str(e), err_type="invalid_request_error")
    except Exception:
        logger.exception("Error parsing upload")
        return _error_json(413, "File too large or invalid upload")

    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    session_locks: dict[str, asyncio.Lock] = _app_value(
        request.app,
        _SESSION_LOCKS_KEY,
        "session_locks",
    )
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info(
        "API request session_key={} media={} text={} stream={}",
        session_key, len(media_paths), text[:80], stream,
    )
    # -- streaming path --
    if stream:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stream_failed = False
        emitted_content = False

        async def _on_stream(token: str) -> None:
            nonlocal emitted_content
            if token:
                emitted_content = True
            await queue.put(token)

        async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
            # Agent stream-end callbacks mark generation segment boundaries.
            # Tool-backed requests may continue after a segment ends, so the
            # HTTP SSE stream is closed only when process_direct returns.
            return None

        async def _run() -> None:
            nonlocal stream_failed
            try:
                async with session_lock:
                    response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                            **_api_command_kwargs(request.app),
                            **_api_tool_kwargs(request.app),
                        ),
                        timeout=timeout_s,
                    )
                    if not emitted_content:
                        response_text = _response_text(response)
                        if response_text.strip():
                            await queue.put(response_text)
            except Exception:
                stream_failed = True
                logger.exception("Streaming error for session {}", session_key)
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        try:
            while True:
                token = await queue.get()
                if token is None:
                    break
                await resp.write(_sse_chunk(token, model_name, chunk_id))
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if not stream_failed:
            await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
            await resp.write(_SSE_DONE)
        return resp

    # -- non-streaming path (original logic) --
    try:
        async with session_lock:
            try:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                        **_api_command_kwargs(request.app),
                        **_api_tool_kwargs(request.app),
                    ),
                    timeout=timeout_s,
                )
                response_text = _response_text(response)
                if not response_text or not response_text.strip():
                    logger.warning("Empty response for session {}, using fallback", session_key)
                    response_text = EMPTY_FINAL_RESPONSE_MESSAGE

            except asyncio.TimeoutError:
                return _error_json(504, f"Request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error processing request for session {}", session_key)
                return _error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected API lock error for session {}", session_key)
        return _error_json(500, "Internal server error", err_type="server_error")

    return web.json_response(
        _chat_completion_response(response_text, model_name, getattr(agent_loop, "_last_usage", None))
    )


async def handle_responses(request: web.Request) -> web.Response:
    """POST /v1/responses - Responses API compatibility with SSE streaming."""
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return _error_json(400, "JSON body must be an object")
    if body.get("stream") not in (None, False, True):
        return _error_json(400, "stream must be a boolean")
    stream = body.get("stream") is True

    model_name: str = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "nanobot")
    requested_model = body.get("model")
    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    try:
        (
            text,
            trusted_instructions,
            previous_response_id,
            initial_session_id,
            requested_owner_id,
        ) = _parse_responses_input(body)
    except ValueError as exc:
        return _error_json(400, str(exc))

    response_sessions: OrderedDict[str, _ResponseSession] = _app_value(
        request.app,
        _RESPONSE_SESSIONS_KEY,
        "response_sessions",
    )
    if previous_response_id:
        previous_session = response_sessions.get(previous_response_id)
        if previous_session is None:
            return _error_json(400, "Unknown previous_response_id")
        if (
            previous_session.owner_id is not None
            and requested_owner_id != previous_session.owner_id
        ):
            return _error_json(403, "Response does not belong to this owner")
        session_key = previous_session.session_key
        owner_id = previous_session.owner_id
        response_sessions.move_to_end(previous_response_id)
    else:
        session_id = initial_session_id or uuid.uuid4().hex
        session_key = f"api:{session_id}"
        owner_id = requested_owner_id

    agent_loop = _app_value(request.app, _AGENT_LOOP_KEY, "agent_loop")
    timeout_s: float = _app_value(
        request.app,
        _REQUEST_TIMEOUT_KEY,
        "request_timeout",
        120.0,
    )
    session_locks: dict[str, asyncio.Lock] = _app_value(
        request.app,
        _SESSION_LOCKS_KEY,
        "session_locks",
    )
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info(
        "Responses API request session_key={} previous_response_id={} text={} stream={}",
        session_key,
        previous_response_id,
        text[:80],
        stream,
    )

    if stream:
        response_id = f"resp_{uuid.uuid4().hex}"
        message_id = f"msg_{uuid.uuid4().hex}"
        created_at = int(time.time())
        sequence_number = 0
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)

        initial_response = _responses_response(
            "",
            model_name,
            response_id,
            previous_response_id,
            message_id=message_id,
            created_at=created_at,
        )
        initial_response["status"] = "in_progress"
        initial_response["output"] = []
        initial_response["usage"] = None

        async def _write_event(event: dict[str, Any]) -> None:
            nonlocal sequence_number
            event["sequence_number"] = sequence_number
            sequence_number += 1
            await resp.write(_responses_sse_event(event))

        await _write_event({"type": "response.created", "response": initial_response})
        await _write_event({"type": "response.in_progress", "response": initial_response})
        await _write_event(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": message_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }
        )
        await _write_event(
            {
                "type": "response.content_part.added",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }
        )

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        emitted_content = False

        async def _on_stream(token: str) -> None:
            nonlocal emitted_content
            if token:
                emitted_content = True
                await queue.put(("delta", token))

        async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
            return None

        async def _run() -> None:
            try:
                async with session_lock:
                    response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                            **_api_command_kwargs(request.app),
                            **_trusted_instruction_kwargs(trusted_instructions),
                            **_api_tool_kwargs(request.app),
                        ),
                        timeout=timeout_s,
                    )
                    response_text = _response_text(response)
                    if not response_text or not response_text.strip():
                        logger.warning("Empty response for session {}, using fallback", session_key)
                        response_text = EMPTY_FINAL_RESPONSE_MESSAGE
                    if not emitted_content:
                        await queue.put(("delta", response_text))
                    await queue.put(("complete", response_text))
            except asyncio.TimeoutError:
                logger.warning("Responses stream timed out for session {}", session_key)
                await queue.put(("error", f"Request timed out after {timeout_s}s"))
            except Exception:
                logger.exception("Streaming Responses error for session {}", session_key)
                await queue.put(("error", "Internal server error"))
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        active_response_tasks: dict[str, _ActiveResponse] = _app_value(
            request.app,
            _ACTIVE_RESPONSE_TASKS_KEY,
            "active_response_tasks",
        )
        active_response_tasks[response_id] = _ActiveResponse(task, owner_id)
        streamed_text = ""
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=_STREAM_HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Keep intermediary proxies active and detect clients that
                    # disconnect while the agent is still executing tools.
                    await resp.write(b": keep-alive\n\n")
                    continue
                if item is None:
                    break
                kind, value = item
                if kind == "delta":
                    streamed_text += value
                    await _write_event(
                        {
                            "type": "response.output_text.delta",
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": value,
                            "logprobs": [],
                        }
                    )
                elif kind == "error":
                    await _write_event(
                        {
                            "type": "error",
                            "code": "request_timeout" if value.startswith("Request timed out") else "server_error",
                            "message": value,
                            "param": None,
                        }
                    )
                else:
                    final_text = streamed_text or value
                    completed_response = _responses_response(
                        final_text,
                        model_name,
                        response_id,
                        previous_response_id,
                        getattr(agent_loop, "_last_usage", None),
                        message_id=message_id,
                        created_at=created_at,
                    )
                    await _write_event(
                        {
                            "type": "response.output_text.done",
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "text": final_text,
                            "logprobs": [],
                        }
                    )
                    await _write_event(
                        {
                            "type": "response.content_part.done",
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": final_text,
                                "annotations": [],
                            },
                        }
                    )
                    await _write_event(
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": completed_response["output"][0],
                        }
                    )
                    response_sessions[response_id] = _ResponseSession(session_key, owner_id)
                    response_sessions.move_to_end(response_id)
                    while len(response_sessions) > _MAX_RESPONSE_SESSIONS:
                        response_sessions.popitem(last=False)
                    await _write_event(
                        {"type": "response.completed", "response": completed_response}
                    )
        except (ConnectionAbortedError, ConnectionResetError):
            logger.info("Responses stream disconnected for session {}", session_key)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            active_response = active_response_tasks.get(response_id)
            if active_response is not None and active_response.task is task:
                active_response_tasks.pop(response_id, None)
        return resp

    try:
        async with session_lock:
            response = await asyncio.wait_for(
                agent_loop.process_direct(
                    content=text,
                    media=None,
                    session_key=session_key,
                    channel="api",
                    chat_id=API_CHAT_ID,
                    **_api_command_kwargs(request.app),
                    **_trusted_instruction_kwargs(trusted_instructions),
                    **_api_tool_kwargs(request.app),
                ),
                timeout=timeout_s,
            )
            response_text = _response_text(response)
            if not response_text or not response_text.strip():
                logger.warning("Empty response for session {}, using fallback", session_key)
                response_text = EMPTY_FINAL_RESPONSE_MESSAGE
    except asyncio.TimeoutError:
        return _error_json(504, f"Request timed out after {timeout_s}s")
    except Exception:
        logger.exception("Error processing Responses request for session {}", session_key)
        return _error_json(500, "Internal server error", err_type="server_error")

    response_id = f"resp_{uuid.uuid4().hex}"
    response_sessions[response_id] = _ResponseSession(session_key, owner_id)
    response_sessions.move_to_end(response_id)
    while len(response_sessions) > _MAX_RESPONSE_SESSIONS:
        response_sessions.popitem(last=False)

    return web.json_response(
        _responses_response(
            response_text,
            model_name,
            response_id,
            previous_response_id,
            getattr(agent_loop, "_last_usage", None),
        )
    )


async def handle_cancel_response(request: web.Request) -> web.Response:
    """Cancel an active Responses API task by response id."""
    response_id = request.match_info["response_id"]
    active_response_tasks: dict[str, _ActiveResponse] = _app_value(
        request.app,
        _ACTIVE_RESPONSE_TASKS_KEY,
        "active_response_tasks",
    )
    active_response = active_response_tasks.get(response_id)
    if active_response is None or active_response.task.done():
        return _error_json(404, "Active response not found")

    owner_id: str | None = None
    raw_body = await request.read()
    if raw_body:
        try:
            body = _json.loads(raw_body)
        except (TypeError, ValueError):
            return _error_json(400, "Invalid JSON body")
        if not isinstance(body, dict):
            return _error_json(400, "JSON body must be an object")
        owner_id = body.get("owner_id")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            return _error_json(400, "owner_id must be a non-empty string")
    if active_response.owner_id is not None and owner_id != active_response.owner_id:
        return _error_json(403, "Response does not belong to this owner")

    task = active_response.task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return web.json_response(
        {
            "id": response_id,
            "object": "response",
            "status": "cancelled",
        }
    )


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models"""
    model_name = _app_value(request.app, _MODEL_NAME_KEY, "model_name", "nanobot")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "nanobot",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop,
    model_name: str = "nanobot",
    request_timeout: float = 120.0,
    api_key: str = "",
    api_tools: ToolRegistry | None = None,
    allow_commands: bool = True,
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
        api_key: Optional API key for Bearer-token authentication on API routes.
        api_tools: Optional restricted registry used by every agent API route.
        allow_commands: Whether API messages may invoke Nanobot slash commands.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for base64 images
    app[_AGENT_LOOP_KEY] = agent_loop
    app[_MODEL_NAME_KEY] = model_name
    app[_REQUEST_TIMEOUT_KEY] = request_timeout
    if api_tools is not None:
        app[_API_TOOLS_KEY] = api_tools
    app[_API_ALLOW_COMMANDS_KEY] = allow_commands
    app[_SESSION_LOCKS_KEY] = {}  # per-user locks, keyed by session_key
    app[_RESPONSE_SESSIONS_KEY] = OrderedDict()
    app[_ACTIVE_RESPONSE_TASKS_KEY] = {}

    @web.middleware
    async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
        # Allow unauthenticated health checks.
        if request.path == "/health":
            return await handler(request)
        if not api_key:
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _error_json(401, "Missing Authorization header. Use: Bearer <api_key>")
        if not hmac.compare_digest(auth[len("Bearer "):], api_key):
            return _error_json(401, "Invalid API key")
        return await handler(request)

    app.middlewares.append(auth_middleware)

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/responses", handle_responses)
    app.router.add_post("/v1/responses/{response_id}/cancel", handle_cancel_response)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app
