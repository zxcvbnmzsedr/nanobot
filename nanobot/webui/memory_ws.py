"""Authenticated WebUI memory management over the gateway WebSocket."""

from __future__ import annotations

from typing import Any

from nanobot.agent.memory_sync import (
    MemorySyncConflictError,
    MemorySyncError,
    MemorySyncPermissionError,
)
from nanobot.identity.principal import Principal

_MAX_REQUEST_ID_LENGTH = 80
_MAX_MEMORY_CONTENT_LENGTH = 64_000
_MEMORY_SCOPES = {"system", "org", "user"}


async def webui_memory_event(
    envelope: dict[str, Any],
    *,
    principal: Principal | None,
    memory_client: Any | None,
) -> tuple[str, dict[str, Any]]:
    """Return a correlated result for one identity-bound memory request."""
    request_id = envelope.get("request_id")
    valid_request_id = (
        isinstance(request_id, str)
        and 0 < len(request_id) <= _MAX_REQUEST_ID_LENGTH
    )

    def error(status: int, detail: str) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {"status": status, "detail": detail}
        if valid_request_id:
            payload["request_id"] = request_id
        return "memory_error", payload

    if not valid_request_id:
        return error(400, "invalid_request")
    if principal is None:
        return error(403, "account_auth_required")
    if memory_client is None:
        return error(503, "memory_unavailable")

    request_type = envelope.get("type")
    try:
        if request_type == "memory_get":
            payload = await memory_client.get_management(principal.user_scope)
        elif request_type == "memory_update":
            scope_type = envelope.get("scopeType")
            content = envelope.get("content")
            expected_version = envelope.get("expectedVersion")
            if (
                scope_type not in _MEMORY_SCOPES
                or not isinstance(content, str)
                or len(content) > _MAX_MEMORY_CONTENT_LENGTH
                or isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 0
            ):
                return error(400, "invalid_request")
            payload = await memory_client.update_management(
                principal.user_scope,
                {
                    "scopeType": scope_type,
                    "content": content,
                    "expectedVersion": expected_version,
                },
            )
        else:
            return error(400, "invalid_request")
    except MemorySyncPermissionError:
        return error(403, "permission_denied")
    except MemorySyncConflictError:
        return error(409, "conflict")
    except MemorySyncError:
        return error(502, "service_unavailable")
    except (TypeError, ValueError):
        return error(502, "invalid_response")

    if not isinstance(payload, dict):
        return error(502, "invalid_response")
    return "memory_result", {"request_id": request_id, "payload": payload}
