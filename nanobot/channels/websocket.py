"""WebSocket server channel: nanobot acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from websockets.asyncio.server import ServerConnection, serve, unix_serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.outbound_events import (
    GoalStateSyncEvent,
    GoalStatusEvent,
    ProgressEvent,
    RuntimeModelUpdatedEvent,
    SessionUpdatedEvent,
    TurnEndEvent,
    outbound_event_from_message,
    outbound_message_for_event,
)
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.identity.principal import IDENTITY_METADATA_KEY, Principal
from nanobot.identity.runtime import TenantRuntime
from nanobot.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScopeError,
)
from nanobot.session.goal_state import goal_state_ws_blob
from nanobot.session.webui_turns import websocket_turn_wall_started_at
from nanobot.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from nanobot.webui.cli_apps_api import normalize_cli_app_mentions
from nanobot.webui.forking import handle_webui_fork_chat
from nanobot.webui.gateway_services import GatewayServices
from nanobot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from nanobot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from nanobot.webui.http_utils import (
    query_first as _query_first,
)
from nanobot.webui.mcp_presets_api import normalize_mcp_preset_mentions
from nanobot.webui.transcription_ws import webui_transcription_event
from nanobot.webui.websocket_logging import websockets_server_logger

# Plain HTTP WebUI routes also run through websockets.process_request.
_WEBUI_HTTP_OPEN_TIMEOUT_S = 360.0
_ACCOUNT_BLOCKED_COMMANDS = {
    "/dream",
    "/dream-log",
    "/dream-prompt",
    "/dream-restore",
    "/model",
    "/pairing",
    "/restart",
}


class KangarooAuthConfig(Base):
    """Exchange trusted Kangaroo accounts for short-lived gateway credentials."""

    enabled: bool = False
    api_base: str = ""
    llm_proxy_url: str = ""
    user_info_path: str = "api/auth/userInfo"
    exchange_path: str = "/api/auth/exchange"
    login_path: str = "/api/auth/login"
    logout_path: str = "/api/auth/logout"
    upstream_login_path: str = "api/auth/oauth/login"
    upstream_refresh_path: str = "api/auth/oauth/token"
    handoff_ttl_s: int = Field(default=60, ge=10, le=600)
    request_timeout_s: float = Field(default=10.0, ge=1.0, le=30.0)
    refresh_skew_s: int = Field(default=3600, ge=30, le=21_600)
    allowed_user_ids: list[str] = Field(default_factory=list)
    runtime_root: str = ""

    @field_validator("exchange_path", "login_path", "logout_path")
    @classmethod
    def exchange_path_format(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('exchange_path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("runtime_root")
    @classmethod
    def runtime_root_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "\x00" in value:
            raise ValueError("runtime_root must not contain NUL bytes")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("runtime_root must be an absolute path")
        return str(path)

    @field_validator("llm_proxy_url")
    @classmethod
    def llm_proxy_url_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("llm_proxy_url must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def require_api_base_when_enabled(self) -> Self:
        if self.enabled and not self.api_base.strip():
            raise ValueError("kangaroo_auth.api_base is required when enabled")
        if self.enabled and not self.llm_proxy_url:
            raise ValueError("kangaroo_auth.llm_proxy_url is required when enabled")
        return self


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    Short-lived tokens may carry a trusted Kangaroo principal; that principal replaces the
    caller-provided client ID and fixes the connection to its account runtime.
    """

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    unix_socket_path: str = ""
    path: str = "/"
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer).
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    kangaroo_auth: KangarooAuthConfig = Field(default_factory=KangarooAuthConfig)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_gateway_key_auth(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        legacy_keys = (
            "token",
            "tokenIssuePath",
            "token_issue_path",
            "tokenIssueSecret",
            "token_issue_secret",
        )
        if any(str(value.get(key) or "").strip() for key in legacy_keys):
            raise ValueError(
                "gateway key authentication has been removed; configure kangarooAuth instead"
            )
        return value

    @field_validator("unix_socket_path")
    @classmethod
    def unix_socket_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "\x00" in value:
            raise ValueError("unix_socket_path must not contain NUL bytes")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("unix_socket_path must be an absolute path")
        return str(path)

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def kangaroo_paths_do_not_conflict(self) -> Self:
        if not self.kangaroo_auth.enabled:
            return self
        if not self.websocket_requires_token:
            raise ValueError(
                "websocket_requires_token must be enabled when kangaroo_auth is enabled"
            )
        exchange_path = self.kangaroo_auth.exchange_path
        login_path = self.kangaroo_auth.login_path
        reserved = {_normalize_config_path(self.path), "/webui/bootstrap"}
        if exchange_path in reserved or login_path in reserved or login_path == exchange_path:
            raise ValueError("kangaroo_auth routes conflict with another gateway route")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.kangaroo_auth.enabled:
            return self
        raise ValueError(
            "host is exposed on all interfaces but kangaroo_auth is not enabled"
        )


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(
        outbound_message_for_event(
            channel="websocket",
            chat_id="*",
            event=RuntimeModelUpdatedEvent(model=model, model_preset=model_preset),
        )
    )


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)*;base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        gateway: GatewayServices,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        self._conn_principals: dict[Any, Principal] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None

        self.gateway = gateway
        self._http_router = gateway.http
        self._tokens = gateway.tokens
        self._media = gateway.media
        self._transcripts = gateway.transcripts
        self._workspaces = gateway.workspaces

        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}

    # -- Subscription bookkeeping -------------------------------------------

    def _workspace_controls_available(self, connection: Any) -> bool:
        if connection in self._conn_principals:
            return False
        return self._http_router.workspace_controls_available(connection)

    def _tenant_runtime(self, connection: Any) -> TenantRuntime | None:
        principal = self._conn_principals.get(connection)
        return self.gateway.tenant_runtimes.for_principal(principal) if principal else None

    def _new_chat_id(self, connection: Any) -> str:
        runtime = self._tenant_runtime(connection)
        return runtime.new_chat_id() if runtime else str(uuid.uuid4())

    def _can_access_chat(self, connection: Any, chat_id: str) -> bool:
        runtime = self._tenant_runtime(connection)
        return runtime.owns_chat_id(chat_id) if runtime else True

    def _persist_tenant_identity(self, connection: Any, chat_id: str) -> None:
        runtime = self._tenant_runtime(connection)
        sessions = self.gateway.session_manager
        if runtime is None or sessions is None:
            return
        session = sessions.get_or_create(f"websocket:{chat_id}")
        session.metadata[IDENTITY_METADATA_KEY] = runtime.identity_metadata()
        sessions.save(session)

    async def _reject_blocked_account_command(
        self,
        connection: Any,
        content: str,
    ) -> bool:
        if self._tenant_runtime(connection) is None:
            return False
        command = content.strip().split(maxsplit=1)[0].lower()
        if command not in _ACCOUNT_BLOCKED_COMMANDS:
            return False
        await self._send_event(connection, "error", detail="account_command_forbidden")
        return True

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)
        self._conn_principals.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed.

        Goal metadata lives on the session JSONL and survives gateway restarts, but
        connected clients normally see it via ``goal_state`` / ``turn_end`` frames.
        Pushing here makes refresh + reconnect restore the strip without a new model turn.
        """
        if self.gateway.session_manager is None:
            return
        row = self.gateway.session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to the HTTP handler or WS upgrade."""
        got, query = _parse_request_path(request.path)

        # WebSocket upgrade — channel handles this itself
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # Everything else goes to the HTTP handler
        return await self._http_router.dispatch(connection, request)

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")

        if self.config.websocket_requires_token:
            grant = self._tokens.take_issued_grant_if_valid(supplied)
            if grant is not None:
                if grant.principal is not None:
                    self._conn_principals[connection] = grant.principal
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._tokens.take_issued_token_if_valid(supplied)
        return None

    # -- Server lifecycle and connection ingress ---------------------------

    async def start(self) -> None:
        from nanobot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")
        ws_logger = websockets_server_logger()

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )
        async def runner() -> None:
            socket_path = self.config.unix_socket_path
            if socket_path:
                path_obj = Path(socket_path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                with suppress(FileNotFoundError):
                    path_obj.unlink()
                server = await unix_serve(
                    handler,
                    socket_path,
                    process_request=process_request,
                    open_timeout=_WEBUI_HTTP_OPEN_TIMEOUT_S,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    logger=ws_logger,
                )
                with suppress(OSError):
                    path_obj.chmod(0o600)
            else:
                server = await serve(
                    handler,
                    self.config.host,
                    self.config.port,
                    process_request=process_request,
                    open_timeout=_WEBUI_HTTP_OPEN_TIMEOUT_S,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    ssl=ssl_context,
                    logger=ws_logger,
                )
            try:
                assert self._stop_event is not None
                await self._stop_event.wait()
            finally:
                server.close()
                await server.wait_closed()
                if socket_path:
                    with suppress(FileNotFoundError):
                        Path(socket_path).unlink()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        principal = self._conn_principals.get(connection)
        client_id_raw = _query_first(query, "client_id")
        client_id = f"kangaroo:{principal.user_id}" if principal else (
            client_id_raw.strip() if client_id_raw else ""
        )
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = self._new_chat_id(connection)
        tenant_runtime = self._tenant_runtime(connection)

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                        **({"identity": principal.public_payload()} if principal else {}),
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                if await self._reject_blocked_account_command(connection, content):
                    continue
                metadata: dict[str, Any] = {
                    "remote": getattr(connection, "remote_address", None),
                }
                if tenant_runtime is not None:
                    scope = tenant_runtime.workspace_scope()
                    metadata[IDENTITY_METADATA_KEY] = tenant_runtime.principal.metadata()
                    metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
                    self._workspaces.persist_scope(default_chat_id, scope)
                    self._persist_tenant_identity(connection, default_chat_id)
                # WebSocket already authenticates at handshake time (token),
                # so pairing is not applicable. Treat as non-DM to avoid
                # sending pairing codes to an already-authenticated client.
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata=metadata,
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    # -- Inbound WebSocket envelopes ---------------------------------------

    def _save_envelope_media(
        self,
        media: list[Any],
        *,
        media_dir: Path | None = None,
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = media_dir or get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = self._new_chat_id(connection)
            runtime = self._tenant_runtime(connection)
            scope = runtime.workspace_scope() if runtime else await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_new_chat(
                    envelope,
                    controls_available=self._workspace_controls_available(connection),
                ),
            )
            if scope is None:
                return
            self._workspaces.persist_scope(new_id, scope)
            self._persist_tenant_identity(connection, new_id)
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=new_id,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "fork_chat":
            source_id = envelope.get("chat_id") or envelope.get("source_chat_id")
            if isinstance(source_id, str) and not self._can_access_chat(connection, source_id):
                await self._send_event(connection, "error", detail="session_forbidden")
                return
            await handle_webui_fork_chat(self, connection, envelope)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not self._can_access_chat(connection, cid):
                await self._send_event(connection, "error", detail="session_forbidden")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "set_workspace_scope":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not self._can_access_chat(connection, cid):
                await self._send_event(connection, "error", detail="session_forbidden")
                return
            if self._tenant_runtime(connection) is not None:
                await self._send_event(
                    connection, "error", detail="workspace_scope_rejected", reason="fixed_account_scope"
                )
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_set_request(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._workspace_controls_available(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return
            self._workspaces.persist_scope(cid, scope)
            self._persist_tenant_identity(connection, cid)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            return
        if t == "transcribe_audio":
            event, payload = await webui_transcription_event(envelope)
            await self._send_event(connection, event, **payload)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not self._can_access_chat(connection, cid):
                await self._send_event(connection, "error", detail="session_forbidden")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return
            if await self._reject_blocked_account_command(connection, content):
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                runtime = self._tenant_runtime(connection)
                media_paths, reason = self._save_envelope_media(
                    raw_media,
                    media_dir=runtime.media if runtime else None,
                )
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            # Allow image-only turns (content may be empty when media is attached).
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return
            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)

            # Resolve after hydration so a concurrent downgrade cannot be overwritten.
            runtime = self._tenant_runtime(connection)
            scope = runtime.workspace_scope() if runtime else await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_message(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._workspace_controls_available(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return

            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if runtime is not None:
                metadata[IDENTITY_METADATA_KEY] = runtime.principal.metadata()
            if envelope.get("webui") is True:
                metadata["webui"] = True
                metadata.update(self._transcripts.client_turn_metadata(envelope.get("turn_id")))
            cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
            if cli_apps:
                metadata["cli_apps"] = cli_apps
            mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
            if mcp_presets:
                metadata["mcp_presets"] = mcp_presets
            metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
            self._workspaces.persist_scope(cid, scope)
            self._persist_tenant_identity(connection, cid)
            if metadata.get("webui") is True and self.is_allowed(client_id):
                self._transcripts.append_user_message(
                    cid,
                    content,
                    metadata=metadata,
                    media_paths=media_paths or None,
                    cli_apps=cli_apps or None,
                    mcp_presets=mcp_presets or None,
                )
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _workspace_scope_or_error(
        self,
        connection: Any,
        resolver: Callable[[], Any],
        *,
        chat_id: str | None = None,
    ) -> Any | None:
        try:
            return resolver()
        except WorkspaceScopeError as exc:
            await self._send_event(
                connection,
                "error",
                detail="workspace_scope_rejected",
                reason=exc.message,
                **({"chat_id": chat_id} if chat_id else {}),
            )
            return None

    # -- Outbound WebSocket events -----------------------------------------

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise
                self.logger.debug("server task was already cancelled during shutdown")
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._conn_principals.clear()
        self._tokens.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        event = outbound_event_from_message(msg)
        progress_event = event if isinstance(event, ProgressEvent) else None
        if isinstance(event, RuntimeModelUpdatedEvent):
            await self.send_runtime_model_updated(
                model_name=event.model,
                model_preset=event.model_preset,
            )
            return

        # Snapshot the subscriber set so ConnectionClosed cleanups mid-iteration are safe.
        conns = list(self._subs.get(msg.chat_id, ()))
        if not conns:
            if isinstance(
                event,
                ProgressEvent
                | TurnEndEvent
                | SessionUpdatedEvent
                | GoalStatusEvent
                | GoalStateSyncEvent,
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
            else:
                self.logger.warning("no active subscribers for chat_id={}", msg.chat_id)
        if isinstance(event, GoalStateSyncEvent):
            if conns:
                await self.send_goal_state(msg.chat_id, event.goal_state or {"active": False})
            return
        if isinstance(event, GoalStatusEvent):
            if conns:
                if event.status in ("running", "idle"):
                    await self.send_goal_status(
                        msg.chat_id,
                        event.status,
                        started_at=event.started_at,
                    )
            return
        # Signal that the agent has fully finished processing the current turn.
        if isinstance(event, TurnEndEvent):
            await self.send_turn_end(
                msg.chat_id,
                latency_ms=event.latency_ms,
                goal_state=event.goal_state,
                metadata=msg.metadata,
            )
            await self.send_session_updated(msg.chat_id, scope="thread")
            return
        if isinstance(event, SessionUpdatedEvent):
            if conns:
                await self.send_session_updated(
                    msg.chat_id,
                    scope=event.scope,
                )
            return
        if progress_event and progress_event.file_edit_events:
            await self.send_file_edit_events(
                msg.chat_id,
                progress_event.file_edit_events,
                msg.metadata,
            )
            return
        text = msg.content
        wire_text = self._media.rewrite_local_markdown_images(text)
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": wire_text,
        }
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._media.sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if progress_event and progress_event.tool_events:
            payload["tool_events"] = progress_event.tool_events
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if progress_event and progress_event.tool_hint:
            payload["kind"] = "tool_hint"
        elif progress_event:
            payload["kind"] = "progress"
        phase = "activity" if payload.get("kind") in ("tool_hint", "progress") else "answer"
        self._transcripts.prepare_and_append(
            msg.chat_id,
            payload,
            metadata=msg.metadata,
            phase=phase,
            include_source=True,
            transcript_overrides={"text": text},
        )
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Push one chunk of model reasoning. Mirrors ``send_delta`` shape so
        clients receive a stream that opens, updates in place, and closes —
        rendered above the active assistant bubble with a shimmer header
        until the matching ``reasoning_end`` arrives.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="reasoning",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
    ) -> None:
        """Close the current reasoning stream segment for in-place renderers."""
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="reasoning",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_file_edit_events(
        self,
        chat_id: str,
        edits: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        payload: dict[str, Any] = {
            "event": "file_edit",
            "chat_id": chat_id,
            "edits": edits,
        }
        self._transcripts.prepare_and_append(
            chat_id,
            payload,
            metadata=metadata,
            phase="activity",
        )
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" file_edit ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        stream_key = (chat_id, str(stream_id or ""))
        if stream_end:
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
            buffered = self._stream_text_buffers.pop(stream_key, [])
            if delta:
                buffered.append(delta)
            full_text = "".join(buffered)
            rewritten = self._media.rewrite_local_markdown_images(full_text)
            if delta or rewritten != full_text:
                body["text"] = rewritten
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="answer",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Signal that the agent has fully finished processing the current turn."""
        conns = list(self._subs.get(chat_id, ()))
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if goal_state is not None:
            body["goal_state"] = goal_state
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=metadata,
            phase="complete",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id* (multi-chat isolation)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """Notify subscribed clients that a turn started or finished (wall-clock hint)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        """Notify WebUI clients that a session row should refresh."""
        conns = list(self._conn_chats)
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        if scope:
            body["scope"] = scope
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")
