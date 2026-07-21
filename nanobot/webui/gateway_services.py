"""Composition helpers for the embedded WebUI gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from loguru import logger as default_logger

from nanobot.agent.memory_sync import KangarooMemoryClient
from nanobot.config.paths import get_data_dir
from nanobot.identity.credentials import KangarooCredentialStore, get_kangaroo_credential_store
from nanobot.identity.handoff import HandoffStore
from nanobot.identity.kangaroo import KangarooIdentityVerifier
from nanobot.identity.runtime import TenantRuntimeStore
from nanobot.webui.gateway_tokens import GatewayTokenStore
from nanobot.webui.media_gateway import WebUIMediaGateway
from nanobot.webui.transcript import WebUITranscriptRecorder
from nanobot.webui.workspaces import WebUIWorkspaceController
from nanobot.webui.ws_http import GatewayHTTPHandler


@dataclass(frozen=True)
class GatewayServices:
    """Explicit dependencies shared by WebSocket transport and HTTP routes."""

    http: GatewayHTTPHandler
    tokens: GatewayTokenStore
    media: WebUIMediaGateway
    transcripts: WebUITranscriptRecorder
    workspaces: WebUIWorkspaceController
    session_manager: Any | None
    cron_service: Any | None
    local_trigger_store: Any | None
    cron_pending_job_ids: Callable[[str], set[str]] | None
    local_trigger_pending_ids: Callable[[str], set[str]] | None
    handoffs: HandoffStore
    identity_verifier: KangarooIdentityVerifier | None
    credential_store: KangarooCredentialStore
    tenant_runtimes: TenantRuntimeStore
    memory_client: KangarooMemoryClient | None


def build_gateway_services(
    *,
    config: Any,
    bus: Any,
    session_manager: Any | None,
    static_dist_path: Path | None,
    workspace_path: Path,
    default_restrict_to_workspace: bool,
    runtime_model_name: Any | None,
    runtime_surface: str,
    runtime_capabilities_overrides: dict[str, Any] | None,
    disabled_skills: set[str] | None = None,
    cron_service: Any | None = None,
    local_trigger_store: Any | None = None,
    cron_pending_job_ids: Callable[[str], set[str]] | None = None,
    local_trigger_pending_ids: Callable[[str], set[str]] | None = None,
    channel_feature_action: Callable[..., Any] | None = None,
    logger: Any = default_logger,
) -> GatewayServices:
    tokens = GatewayTokenStore()
    handoffs = HandoffStore()
    auth_config = config.kangaroo_auth
    runtime_root = (
        Path(auth_config.runtime_root).expanduser()
        if auth_config.runtime_root.strip()
        else get_data_dir() / "tenants"
    )
    tenant_runtimes = TenantRuntimeStore(runtime_root)
    identity_verifier = (
        KangarooIdentityVerifier(
            api_base=auth_config.api_base,
            user_info_path=auth_config.user_info_path,
            login_path=auth_config.upstream_login_path,
            refresh_path=auth_config.upstream_refresh_path,
            timeout_s=auth_config.request_timeout_s,
            allowed_user_ids={str(value).strip() for value in auth_config.allowed_user_ids},
        )
        if auth_config.enabled
        else None
    )
    credential_store = get_kangaroo_credential_store()
    if identity_verifier is not None:
        credential_store.configure(
            persistence_path=runtime_root / ".kangaroo-credentials.enc",
            refresher=identity_verifier.refresh,
            refresh_skew_s=auth_config.refresh_skew_s,
        )
    memory_client = (
        KangarooMemoryClient(
            base_url=auth_config.memory_api_url,
            credential_store=credential_store,
            timeout_s=auth_config.request_timeout_s,
        )
        if auth_config.enabled and auth_config.memory_api_url
        else None
    )
    media = WebUIMediaGateway(
        workspace_path=workspace_path,
        logger=logger,
    )
    transcripts = WebUITranscriptRecorder(log=logger)
    workspaces = WebUIWorkspaceController(
        session_manager=session_manager,
        default_workspace=workspace_path,
        default_restrict_to_workspace=default_restrict_to_workspace,
    )
    http = GatewayHTTPHandler(
        config=config,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        runtime_model_name=runtime_model_name,
        runtime_surface=runtime_surface,
        runtime_capabilities_overrides=runtime_capabilities_overrides,
        bus=bus,
        tokens=tokens,
        handoffs=handoffs,
        identity_verifier=identity_verifier,
        credential_store=credential_store,
        tenant_runtimes=tenant_runtimes,
        media=media,
        workspaces=workspaces,
        skills_workspace_path=workspace_path,
        disabled_skills=disabled_skills,
        cron_service=cron_service,
        local_trigger_store=local_trigger_store,
        cron_pending_job_ids=cron_pending_job_ids,
        local_trigger_pending_ids=local_trigger_pending_ids,
        channel_feature_action=channel_feature_action,
        log=logger,
    )
    return GatewayServices(
        http=http,
        tokens=tokens,
        media=media,
        transcripts=transcripts,
        workspaces=workspaces,
        session_manager=session_manager,
        cron_service=cron_service,
        local_trigger_store=local_trigger_store,
        cron_pending_job_ids=cron_pending_job_ids,
        local_trigger_pending_ids=local_trigger_pending_ids,
        handoffs=handoffs,
        identity_verifier=identity_verifier,
        credential_store=credential_store,
        tenant_runtimes=tenant_runtimes,
        memory_client=memory_client,
    )
