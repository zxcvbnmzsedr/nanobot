"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any, Mapping, Sequence

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools import mcp as mcp_tools
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.apps.cli import utils as cli_app_utils
from nanobot.bus.events import InboundMessage
from nanobot.identity.principal import IDENTITY_METADATA_KEY
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_END,
    RUNTIME_CONTEXT_MESSAGE_META,
    RUNTIME_CONTEXT_TAG,
    RuntimeContextBlock,
    append_runtime_context,
)
from nanobot.skill_market.models import SkillSnapshot
from nanobot.skill_market.provenance import filter_revoked_skill_history
from nanobot.skill_market.store import (
    ManagedSkillStore,
    managed_skills_path_from_metadata,
    pin_snapshot_in_metadata,
    snapshot_from_metadata,
)
from nanobot.utils.helpers import (
    detect_image_mime,
    load_bundled_template,
    truncate_text_to_tokens,
)
from nanobot.utils.prompt_templates import render_template


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return persisted kwargs for turn-attached capabilities."""
    return cli_app_utils.session_extra(metadata) | mcp_tools.session_extra(metadata)


async def connect_mcp(state: Any, tools: ToolRegistry) -> None:
    await mcp_tools.connect_missing_servers(state, tools)


async def close_mcp(state: Any) -> None:
    await mcp_tools.close_mcp_servers(state)


async def handle_runtime_control(state: Any, msg: InboundMessage, tools: ToolRegistry) -> bool:
    return await mcp_tools.handle_runtime_control(state, msg, tools)


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]
    _RUNTIME_CONTEXT_TAG = RUNTIME_CONTEXT_TAG
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_TOKENS = 8_000  # hard cap on recent history section size (tokens)
    _RUNTIME_CONTEXT_END = RUNTIME_CONTEXT_END

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self._workspace_memories: dict[Path, MemoryStore] = {workspace.resolve(): self.memory}
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        session_metadata: Mapping[str, Any] | None = None,
        skill_snapshot: SkillSnapshot | Mapping[str, Any] | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        root = workspace or self.workspace
        parts = [self._get_identity(channel=channel, workspace=root)]

        bootstrap = self._load_bootstrap_files(root)
        if bootstrap:
            parts.append(bootstrap)

        parts.append(render_template("agent/tool_contract.md"))

        memory_sections = self._memory_sections(root, session_metadata)
        if memory_sections:
            parts.append("# Memory\n\n" + "\n\n".join(memory_sections))

        skills = self.skills_for_workspace(
            root,
            session_metadata=session_metadata,
            skill_snapshot=skill_snapshot,
        )
        always_skills = skills.get_always_skills()
        if always_skills:
            always_content = skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        if include_memory_recent_history:
            identity = (
                session_metadata.get(IDENTITY_METADATA_KEY)
                if isinstance(session_metadata, Mapping)
                else None
            )
            prompt_memory = (
                self.memory_for_workspace(root) if isinstance(identity, Mapping) else self.memory
            )
            entries = prompt_memory.read_recent_history_for_prompt(
                since_cursor=prompt_memory.get_last_dream_cursor(),
                session_key=session_key,
                unified_session=unified_session,
            )
            if entries:
                capped = entries[-self._MAX_RECENT_HISTORY:]
                history_text = "\n".join(
                    f"- [{e['timestamp']}] {e['content']}" for e in capped
                )
                history_text = truncate_text_to_tokens(history_text, self._MAX_HISTORY_TOKENS)
                parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def skills_for_workspace(
        self,
        workspace: Path,
        *,
        session_metadata: Mapping[str, Any] | None = None,
        skill_snapshot: SkillSnapshot | Mapping[str, Any] | None = None,
    ) -> SkillsLoader:
        """Build a tenant-aware loader pinned to one managed Skill snapshot."""
        metadata = pin_snapshot_in_metadata(session_metadata)
        managed_root = managed_skills_path_from_metadata(metadata)
        snapshot = (
            skill_snapshot
            if skill_snapshot is not None
            else snapshot_from_metadata(metadata)
        )
        root = workspace.expanduser().resolve(strict=False)
        if root == self.workspace.expanduser().resolve(strict=False) and managed_root is None:
            return self.skills
        return SkillsLoader(
            root,
            builtin_skills_dir=self.skills.builtin_skills,
            disabled_skills=set(self.skills.disabled_skills),
            managed_skills_root=managed_root,
            managed_snapshot=snapshot,
        )

    def memory_for_workspace(self, workspace: Path) -> MemoryStore:
        root = workspace.expanduser().resolve(strict=False)
        store = self._workspace_memories.get(root)
        if store is None:
            store = MemoryStore(root)
            self._workspace_memories[root] = store
        return store

    def _memory_sections(
        self,
        workspace: Path,
        session_metadata: Mapping[str, Any] | None,
    ) -> list[str]:
        sections: list[str] = []
        system_memory = self.memory.read_memory()
        identity = (
            session_metadata.get(IDENTITY_METADATA_KEY)
            if isinstance(session_metadata, Mapping)
            else None
        )
        if not isinstance(identity, Mapping):
            legacy = self.memory.get_memory_context()
            if system_memory and not self._is_template_content(system_memory, "memory/MEMORY.md"):
                return [legacy]
            return []
        if system_memory and not self._is_template_content(system_memory, "memory/MEMORY.md"):
            sections.append(f"## System Memory\n{system_memory}")

        if isinstance(identity, Mapping):
            org_path = identity.get("org_memory_path")
            if isinstance(org_path, str) and org_path:
                org_memory = MemoryStore.read_file(Path(org_path))
                if org_memory:
                    sections.append(f"## Organization Memory\n{org_memory}")

        user_store = self.memory_for_workspace(workspace)
        if user_store is not self.memory:
            user_memory = user_store.read_memory()
            if user_memory:
                sections.append(f"## User Memory\n{user_memory}")
        return sections

    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        """Get the core identity section."""
        root = workspace or self.workspace
        workspace_path = str(root.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            if not left:
                return right
            if not right:
                return left
            return f"{left}\n\n{right}"

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, workspace: Path | None = None) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        root = workspace or self.workspace

        for filename in self.BOOTSTRAP_FILES:
            file_path = root / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        tpl = load_bundled_template(template_path)
        if tpl is not None:
            return content.strip() == tpl.strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        runtime_context_blocks: Sequence[RuntimeContextBlock] | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        skill_snapshot: SkillSnapshot | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        root = workspace or self.workspace
        user_content = self._build_user_content(current_message, media)
        blocks = list(runtime_context_blocks or ()) if current_role == "user" else []
        merged, runtime_context_meta = append_runtime_context(user_content, blocks)
        metadata = pin_snapshot_in_metadata(session_metadata)
        managed_root = managed_skills_path_from_metadata(metadata)
        replay_history = filter_revoked_skill_history(
            history,
            ManagedSkillStore(managed_root) if managed_root is not None else None,
        )
        pinned_snapshot = skill_snapshot or snapshot_from_metadata(metadata)
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    workspace=root,
                    include_memory_recent_history=include_memory_recent_history,
                    session_key=session_key,
                    unified_session=unified_session,
                    session_metadata=metadata,
                    skill_snapshot=pinned_snapshot,
                ),
            },
            *replay_history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            if current_role == "user" and runtime_context_meta is not None:
                internal_meta = dict(last.get("_meta") or {})
                internal_meta[RUNTIME_CONTEXT_MESSAGE_META] = runtime_context_meta
                last["_meta"] = internal_meta
            messages[-1] = last
            return messages
        current = {"role": current_role, "content": merged}
        if current_role == "user" and runtime_context_meta is not None:
            current["_meta"] = {RUNTIME_CONTEXT_MESSAGE_META: runtime_context_meta}
        messages.append(current)
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
