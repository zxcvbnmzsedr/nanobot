import base64
import json
from pathlib import Path

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from nanobot.agent.context import ContextBuilder
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketConfig
from nanobot.identity.credentials import get_kangaroo_credential_store
from nanobot.identity.handoff import HandoffStore
from nanobot.identity.kangaroo import AuthenticatedKangarooIdentity
from nanobot.identity.principal import IDENTITY_METADATA_KEY, Principal
from nanobot.identity.runtime import TenantRuntimeStore
from nanobot.webui.gateway_services import build_gateway_services
from nanobot.webui.gateway_tokens import GatewayTokenStore


def _principal(user_id: str = "101", org_id: str = "9001") -> Principal:
    return Principal(user_id=user_id, org_id=org_id, name="Test User")


def test_handoff_is_one_time_and_tokens_keep_the_principal() -> None:
    principal = _principal()
    handoffs = HandoffStore()
    code = handoffs.issue(principal, 60)

    assert handoffs.consume(code) == principal
    assert handoffs.consume(code) is None

    tokens = GatewayTokenStore()
    token = tokens.issue_token(60, principal)
    grant = tokens.take_issued_grant_if_valid(token)

    assert grant is not None
    assert grant.principal == principal
    assert tokens.take_issued_grant_if_valid(token) is None


def test_tenant_runtime_separates_users_and_shares_org_memory(tmp_path: Path) -> None:
    store = TenantRuntimeStore(tmp_path / "tenants")
    first = store.for_principal(_principal("101", "9001"))
    second = store.for_principal(_principal("102", "9001"))

    assert first.workspace != second.workspace
    assert first.user_memory != second.user_memory
    assert first.org_memory == second.org_memory
    assert first.workspace_scope().restrict_to_workspace is True
    assert first.owns_chat_id(first.new_chat_id())
    assert not first.owns_chat_id(second.new_chat_id())


def test_context_reads_system_org_and_user_memory(tmp_path: Path) -> None:
    system_workspace = tmp_path / "system"
    system_workspace.mkdir()
    builder = ContextBuilder(system_workspace)
    builder.memory.write_memory("system fact")

    runtime = TenantRuntimeStore(tmp_path / "tenants").for_principal(_principal())
    runtime.user_memory.write_text("private fact", encoding="utf-8")
    runtime.org_memory.write_text("shared org fact", encoding="utf-8")

    prompt = builder.build_system_prompt(
        workspace=runtime.workspace,
        session_metadata={IDENTITY_METADATA_KEY: runtime.identity_metadata()},
    )

    assert "## System Memory\nsystem fact" in prompt
    assert "## Organization Memory\nshared org fact" in prompt
    assert "## User Memory\nprivate fact" in prompt


def test_context_keeps_legacy_memory_for_untrusted_workspace(tmp_path: Path) -> None:
    system_workspace = tmp_path / "system"
    alternate_workspace = tmp_path / "alternate"
    system_workspace.mkdir()
    alternate_workspace.mkdir()
    builder = ContextBuilder(system_workspace)
    builder.memory.write_memory("system fact")
    builder.memory.append_history("system history", session_key="websocket:legacy")
    alternate_memory = builder.memory_for_workspace(alternate_workspace)
    alternate_memory.write_memory("alternate fact")
    alternate_memory.append_history("alternate history", session_key="websocket:legacy")

    prompt = builder.build_system_prompt(
        workspace=alternate_workspace,
        session_key="websocket:legacy",
    )

    assert "system fact" in prompt
    assert "system history" in prompt
    assert "alternate fact" not in prompt
    assert "alternate history" not in prompt


@pytest.mark.parametrize(
    "websocket_overrides",
    [
        {"websocketRequiresToken": False},
        {"tokenIssuePath": "/token"},
    ],
)
def test_kangaroo_auth_rejects_principal_bypass_config(
    websocket_overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        WebSocketConfig.model_validate({
            **websocket_overrides,
            "kangarooAuth": {
                "enabled": True,
                "apiBase": "https://accounts.example.com/",
                "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
            },
        })


def test_kangaroo_auth_requires_llm_proxy_url() -> None:
    with pytest.raises(ValueError, match="llm_proxy_url"):
        WebSocketConfig.model_validate({
            "kangarooAuth": {
                "enabled": True,
                "apiBase": "https://accounts.example.com/",
            },
        })


@pytest.mark.asyncio
async def test_exchange_and_bootstrap_keep_the_verified_identity(tmp_path: Path) -> None:
    config = WebSocketConfig.model_validate({
        "path": "/ws",
        "kangarooAuth": {
            "enabled": True,
            "apiBase": "https://accounts.example.com/",
            "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
            "runtimeRoot": str(tmp_path / "tenants"),
        },
    })
    gateway = build_gateway_services(
        config=config,
        bus=MessageBus(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path / "system",
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    principal = _principal()
    credential_store = get_kangaroo_credential_store()
    credential_store.clear()

    class Verifier:
        async def verify(self, access_token: str) -> Principal:
            assert access_token == "kangaroo-token"
            return principal

    gateway.http.identity_verifier = Verifier()  # type: ignore[assignment]
    exchange = await gateway.http._handle_kangaroo_exchange(Request(
        "/api/auth/exchange",
        Headers({"Authorization": "Bearer kangaroo-token"}),
    ))
    exchange_body = json.loads(exchange.body)
    assert exchange.headers["Cache-Control"] == "no-store"

    class RemoteConnection:
        remote_address = ("203.0.113.10", 1234)

    bootstrap = gateway.http._handle_bootstrap(
        RemoteConnection(),
        Request(
            "/webui/bootstrap",
            Headers({"X-Nanobot-Handoff": exchange_body["handoff_code"]}),
        ),
    )
    bootstrap_body = json.loads(bootstrap.body)

    assert bootstrap_body["identity"]["userId"] == "101"
    assert bootstrap.headers["Cache-Control"] == "no-store"
    grant = gateway.tokens.take_issued_grant_if_valid(bootstrap_body["token"])
    assert grant is not None and grant.principal == principal
    assert credential_store.get(principal.user_scope) == "kangaroo-token"
    credential_store.clear()


@pytest.mark.asyncio
async def test_native_login_returns_identity_handoff_without_upstream_tokens(
    tmp_path: Path,
) -> None:
    config = WebSocketConfig.model_validate({
        "path": "/ws",
        "kangarooAuth": {
            "enabled": True,
            "apiBase": "https://accounts.example.com/",
            "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
            "runtimeRoot": str(tmp_path / "tenants"),
        },
    })
    gateway = build_gateway_services(
        config=config,
        bus=MessageBus(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path / "system",
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    principal = _principal()
    credential_store = get_kangaroo_credential_store()
    credential_store.clear()

    class Verifier:
        async def login_with_access_token(
            self,
            username: str,
            password: str,
        ) -> AuthenticatedKangarooIdentity:
            assert (username, password) == ("13800138000", "secret-password")
            return AuthenticatedKangarooIdentity(principal, "native-login-token")

    class RemoteConnection:
        remote_address = ("203.0.113.10", 1234)

    gateway.http.identity_verifier = Verifier()  # type: ignore[assignment]
    encoded = base64.b64encode(b"13800138000:secret-password").decode()
    response = await gateway.http._handle_kangaroo_login(
        RemoteConnection(),
        Request("/api/auth/login", Headers({"Authorization": f"Basic {encoded}"})),
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert body["user"]["userId"] == "101"
    assert body["handoff_code"].startswith("nbho_")
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert credential_store.get(principal.user_scope) == "native-login-token"

    bootstrap = gateway.http._handle_bootstrap(
        RemoteConnection(),
        Request(
            "/webui/bootstrap",
            Headers({"X-Nanobot-Handoff": body["handoff_code"]}),
        ),
    )
    api_token = json.loads(bootstrap.body)["api_token"]
    logout = gateway.http._handle_kangaroo_logout(
        Request(
            "/api/auth/logout",
            Headers({"Authorization": f"Bearer {api_token}"}),
        )
    )
    assert logout.status_code == 200
    assert credential_store.get(principal.user_scope) is None
    assert not gateway.tokens.check_api_token(
        Request(
            "/api/sessions",
            Headers({"Authorization": f"Bearer {api_token}"}),
        )
    )
    credential_store.clear()


def test_kangaroo_mode_disables_localhost_bootstrap_bypass(tmp_path: Path) -> None:
    config = WebSocketConfig.model_validate({
        "kangarooAuth": {
            "enabled": True,
            "apiBase": "https://accounts.example.com/",
            "llmProxyUrl": "https://agent.example.com/nanobot/llm/stream",
            "runtimeRoot": str(tmp_path / "tenants"),
        },
    })
    gateway = build_gateway_services(
        config=config,
        bus=MessageBus(),
        session_manager=None,
        static_dist_path=None,
        workspace_path=tmp_path / "system",
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )

    class LocalConnection:
        remote_address = ("127.0.0.1", 1234)

    response = gateway.http._handle_bootstrap(
        LocalConnection(),
        Request("/webui/bootstrap", Headers({"Host": "127.0.0.1:8765"})),
    )

    assert response.status_code == 401
    assert json.loads(response.body)["auth_mode"] == "kangaroo"
