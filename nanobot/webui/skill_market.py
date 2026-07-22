"""Principal-bound Skill marketplace contracts for the embedded WebUI."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from nanobot.identity.principal import Principal

_MAX_REQUEST_ID_LENGTH = 80
_SKILL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.+_-]{0,62}[A-Za-z0-9])?$")
_UPDATE_POLICIES = {"manual", "notify", "auto_stable", "pinned"}
_SENSITIVE_KEYS = {
    "access_token",
    "artifact_key",
    "artifact_path",
    "artifact_url",
    "download_url",
    "file_path",
    "files",
    "local_path",
    "manifest",
    "raw_markdown",
    "refresh_token",
    "relative_path",
    "storage_key",
    "token",
    "workspace_path",
    "body",
    "content",
}


class SkillMarketServiceProtocol(Protocol):
    """Operations the gateway expects from the code-level Skill manager."""

    async def catalog(self, principal: Principal) -> Any: ...

    async def detail(self, principal: Principal, skill_id: str) -> Any: ...

    async def inventory(self, principal: Principal) -> Any: ...

    async def install(self, principal: Principal, skill_id: str, **options: Any) -> Any: ...

    async def update(self, principal: Principal, skill_id: str, **options: Any) -> Any: ...

    async def rollback(self, principal: Principal, skill_id: str, **options: Any) -> Any: ...

    async def uninstall(self, principal: Principal, skill_id: str, **options: Any) -> Any: ...

    async def set_policy(self, principal: Principal, skill_id: str, **options: Any) -> Any: ...

    async def sync_now(self, principal: Principal, **options: Any) -> Any: ...

    async def start(self, principal: Principal) -> None: ...

    async def stop(self) -> None: ...

    def subscribe(
        self,
        callback: Callable[[dict[str, Any]], None | Awaitable[None]],
    ) -> Callable[[], None]: ...

    def active_entries(self, principal: Principal) -> list[dict[str, Any]]: ...


def public_skill_payload(value: Any) -> Any:
    """Return JSON-safe marketplace data with local capabilities removed.

    The control-plane DTO is intentionally allowed to evolve. This boundary
    recursively removes fields that could expose credentials, local paths, or
    direct artifact locations and converts field names to the WebUI's camelCase
    convention.
    """
    if hasattr(value, "model_dump") and callable(value.model_dump):
        value = value.model_dump(mode="json")
    elif hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        value = asdict(value)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                continue
            snake_key = _snake_key(raw_key)
            if _is_sensitive_key(snake_key):
                continue
            result[_camel_key(snake_key)] = public_skill_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        return [public_skill_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def skill_market_error(exc: Exception) -> tuple[int, str, str, bool]:
    """Map service failures to a stable, non-sensitive browser error."""
    status = getattr(exc, "http_status", None) or getattr(exc, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        if isinstance(exc, PermissionError):
            status = 403
        elif isinstance(exc, (TypeError, ValueError)):
            status = 400
        else:
            status = 502
    if status < 400 or status > 599:
        status = 502

    raw_code = getattr(exc, "code", None)
    code = raw_code if isinstance(raw_code, str) and raw_code else _default_error_code(status)
    raw_detail = getattr(exc, "detail", None)
    detail = raw_detail if isinstance(raw_detail, str) and raw_detail else code.lower()
    retryable = bool(getattr(exc, "retryable", status in {429, 502, 503, 504}))
    return status, code[:80], detail[:240], retryable


async def skill_market_read(
    service: SkillMarketServiceProtocol | None,
    principal: Principal | None,
    operation: str,
    *,
    skill_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute one authenticated read and return an HTTP status and payload."""
    if principal is None:
        return 403, _error_payload(403, "ACCOUNT_AUTH_REQUIRED", "account_auth_required")
    if service is None:
        return 503, _error_payload(503, "SKILL_MARKET_UNAVAILABLE", "skill_market_unavailable")
    if skill_id is not None and not _valid_skill_id(skill_id):
        return 400, _error_payload(400, "INVALID_REQUEST", "invalid_skill_id")

    try:
        if operation == "catalog":
            result = await service.catalog(principal)
        elif operation == "detail" and skill_id is not None:
            result = await service.detail(principal, skill_id)
        elif operation == "inventory":
            result = await service.inventory(principal)
        elif operation == "status":
            status_descriptor = inspect.getattr_static(service, "status", None)
            if callable(status_descriptor):
                result = await _await_result(getattr(service, "status")(principal))
            else:
                inventory = public_skill_payload(await service.inventory(principal))
                remote_available = (
                    inventory.get("remoteAvailable")
                    if isinstance(inventory, dict)
                    else None
                )
                result = {
                    "enabled": True,
                    "available": remote_available if isinstance(remote_available, bool) else True,
                }
                if isinstance(inventory, dict):
                    local = inventory.get("local")
                    sync = local.get("sync") if isinstance(local, dict) else None
                    for key in (
                        "snapshotId",
                        "revision",
                        "desiredRevision",
                        "lastSyncedAt",
                        "nextSyncAt",
                        "syncStatus",
                    ):
                        if key in inventory:
                            result[key] = inventory[key]
                        elif isinstance(local, dict) and key in local:
                            result[key] = local[key]
                    if isinstance(sync, dict):
                        if sync.get("lastSuccessAt") is not None:
                            result["lastSyncedAt"] = sync["lastSuccessAt"]
                        if sync.get("lastStatus") is not None:
                            result["syncStatus"] = sync["lastStatus"]
                        if isinstance(sync.get("lkgFresh"), bool):
                            result["stale"] = not sync["lkgFresh"]
                        if sync.get("staleSince") is not None:
                            result["staleSince"] = sync["staleSince"]
                        if sync.get("lastErrorCode"):
                            result["lastErrorCode"] = sync["lastErrorCode"]
                    if remote_available is False and inventory.get("remoteErrorCode"):
                        result["lastErrorCode"] = inventory["remoteErrorCode"]
        else:
            return 404, _error_payload(404, "NOT_FOUND", "not_found")
    except Exception as exc:
        status, code, detail, retryable = skill_market_error(exc)
        return status, _error_payload(status, code, detail, retryable=retryable)

    payload = public_skill_payload(result)
    if not isinstance(payload, dict):
        return 502, _error_payload(502, "INVALID_RESPONSE", "invalid_response")
    return 200, payload


async def webui_skill_market_event(
    envelope: dict[str, Any],
    *,
    principal: Principal | None,
    service: SkillMarketServiceProtocol | None,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Execute one correlated Skill mutation and describe any org broadcast."""
    request_id = envelope.get("request_id")
    valid_request_id = (
        isinstance(request_id, str) and 0 < len(request_id) <= _MAX_REQUEST_ID_LENGTH
    )

    def error(
        status: int,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> tuple[str, dict[str, Any], None]:
        payload = _error_payload(status, code, detail, retryable=retryable)
        if valid_request_id:
            payload["request_id"] = request_id
        return "skill_operation_error", payload, None

    if not valid_request_id:
        return error(400, "INVALID_REQUEST", "invalid_request")
    if principal is None:
        return error(403, "ACCOUNT_AUTH_REQUIRED", "account_auth_required")
    if service is None:
        return error(503, "SKILL_MARKET_UNAVAILABLE", "skill_market_unavailable", retryable=True)

    request_type = envelope.get("type")
    operation_by_type = {
        "skill_install": "install",
        "skill_update": "update",
        "skill_rollback": "rollback",
        "skill_uninstall": "uninstall",
        "skill_set_update_policy": "set_policy",
        "skill_sync_now": "sync_now",
    }
    operation = operation_by_type.get(request_type)
    if operation is None:
        return error(400, "INVALID_REQUEST", "invalid_request")

    skill_id: str | None = None
    if operation != "sync_now":
        raw_skill_id = envelope.get("skillId")
        if not isinstance(raw_skill_id, str) or not _valid_skill_id(raw_skill_id):
            return error(400, "INVALID_REQUEST", "invalid_skill_id")
        skill_id = raw_skill_id

    options: dict[str, Any] = {}
    version = envelope.get("version")
    if version is not None:
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            return error(400, "INVALID_REQUEST", "invalid_version")
        options["version"] = version
    if operation == "rollback" and "version" not in options:
        return error(400, "INVALID_REQUEST", "version_required")

    expected_revision = envelope.get("expectedRowVersion", envelope.get("expectedRevision"))
    if expected_revision is not None:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return error(400, "INVALID_REQUEST", "invalid_expected_revision")
        options["expected_row_version"] = expected_revision

    policy = envelope.get("updatePolicy")
    if policy is not None:
        if policy not in _UPDATE_POLICIES:
            return error(400, "INVALID_REQUEST", "invalid_update_policy")
        options["update_policy"] = policy
    if operation == "set_policy" and "update_policy" not in options:
        return error(400, "INVALID_REQUEST", "update_policy_required")

    try:
        method = getattr(service, operation)
        if skill_id is None:
            result = await _await_result(method(principal, **options))
        else:
            result = await _await_result(method(principal, skill_id, **options))
    except Exception as exc:
        status, code, detail, retryable = skill_market_error(exc)
        return error(status, code, detail, retryable=retryable)

    payload = public_skill_payload(result)
    if not isinstance(payload, dict):
        return error(502, "INVALID_RESPONSE", "invalid_response")
    response = {"request_id": request_id, "payload": payload}
    broadcast: dict[str, Any] = {"reason": operation}
    if skill_id is not None:
        broadcast["skill_id"] = skill_id
    for key in ("snapshotId", "revision", "desiredRevision"):
        if key in payload:
            broadcast[key] = payload[key]
    return "skill_operation_result", response, broadcast


async def _await_result(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _valid_skill_id(value: str) -> bool:
    return _SKILL_ID_RE.fullmatch(value) is not None


def _snake_key(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).replace("-", "_").lower()


def _camel_key(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def _is_sensitive_key(key: str) -> bool:
    return (
        key in _SENSITIVE_KEYS
        or key.endswith("_token")
        or key.endswith("_local_path")
        or key.endswith("_artifact_url")
    )


def _default_error_code(status: int) -> str:
    return {
        400: "INVALID_REQUEST",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "SKILL_NOT_FOUND",
        409: "REVISION_CONFLICT",
        429: "RATE_LIMITED",
        503: "SKILL_MARKET_UNAVAILABLE",
    }.get(status, "SERVICE_UNAVAILABLE")


def _error_payload(
    status: int,
    code: str,
    detail: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "detail": detail,
        "retryable": retryable,
    }
