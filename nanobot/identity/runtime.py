"""Filesystem layout and ownership rules for authenticated account runtimes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from nanobot.identity.principal import Principal
from nanobot.security.workspace_access import WorkspaceScope, build_workspace_scope
from nanobot.utils.helpers import ensure_dir


@dataclass(frozen=True, slots=True)
class TenantRuntime:
    principal: Principal
    root: Path
    workspace: Path
    user_memory: Path
    org_memory: Path
    managed_skills: Path
    media: Path
    webui: Path

    @property
    def chat_prefix(self) -> str:
        return f"u{self.principal.user_scope[:12]}_"

    def new_chat_id(self) -> str:
        return f"{self.chat_prefix}{uuid.uuid4().hex}"

    def owns_chat_id(self, chat_id: str) -> bool:
        return chat_id.startswith(self.chat_prefix)

    def workspace_scope(self) -> WorkspaceScope:
        return build_workspace_scope(self.workspace, "restricted", source_channel="websocket")

    def identity_metadata(self) -> dict[str, object]:
        return {
            **self.principal.metadata(),
            "runtime_root": str(self.root),
            "user_memory_path": str(self.user_memory),
            "org_memory_path": str(self.org_memory),
            "managed_skills_path": str(self.managed_skills),
        }


class TenantRuntimeStore:
    """Resolve deterministic account directories under one configured root."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)

    def for_principal(self, principal: Principal) -> TenantRuntime:
        user_root = ensure_dir(self.root / "users" / principal.user_scope)
        org_root = ensure_dir(self.root / "organizations" / principal.org_scope)
        workspace = ensure_dir(user_root / "workspace")
        user_memory = ensure_dir(workspace / "memory") / "MEMORY.md"
        org_memory = ensure_dir(org_root / "memory") / "MEMORY.md"
        return TenantRuntime(
            principal=principal,
            root=user_root,
            workspace=workspace,
            user_memory=user_memory,
            org_memory=org_memory,
            managed_skills=ensure_dir(org_root / "managed-skills"),
            media=ensure_dir(user_root / "media"),
            webui=ensure_dir(user_root / "webui"),
        )
