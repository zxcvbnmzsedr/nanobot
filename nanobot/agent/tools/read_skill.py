"""Read-only access to a turn-pinned, signed managed Skill release."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.skill_market.errors import SkillMarketError
from nanobot.skill_market.provenance import add_provenance
from nanobot.skill_market.store import (
    ManagedSkillStore,
    managed_skills_path_from_metadata,
    snapshot_from_metadata,
)


@tool_parameters(
    tool_parameters_schema(
        skill_name=StringSchema("Installed managed Skill name"),
        reference=StringSchema(
            "Optional text file within references/; defaults to SKILL.md",
        ),
        required=["skill_name"],
    )
)
class ReadSkillTool(Tool):
    """Read signed managed Skill instructions without granting filesystem access."""

    _scopes = {"core", "subagent"}
    _MAX_CHARS = 128_000

    @property
    def name(self) -> str:
        return "read_skill"

    @property
    def description(self) -> str:
        return (
            "Read an installed, signed managed Skill or one of its text references. "
            "Use the exact Skill name from the available-skills list. This tool is read-only "
            "and stays pinned to the Skill snapshot selected at the start of the turn."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        skill_name: str,
        reference: str | None = None,
        **kwargs: Any,
    ) -> str:
        context = current_request_context()
        if context is None:
            return ToolResult.error("Error: read_skill requires an active request")
        root = managed_skills_path_from_metadata(context.metadata)
        snapshot = snapshot_from_metadata(context.metadata)
        if root is None or snapshot is None:
            return ToolResult.error("Error: no managed Skill snapshot is available")
        store = ManagedSkillStore(root)
        relative = reference or "SKILL.md"
        if reference and not reference.startswith("references/"):
            relative = f"references/{reference}"
        pinned_skill = snapshot.skills.get(skill_name)
        try:
            if pinned_skill is not None and pinned_skill.sha256 in store.revoked_digests():
                return ToolResult.error("Error: this managed Skill release has been revoked")
            skill, path = store.resolve_skill_file(snapshot, skill_name, relative)
            if skill.sha256 in store.revoked_digests():
                return ToolResult.error("Error: this managed Skill release has been revoked")
        except SkillMarketError as exc:
            return ToolResult.error(f"Error: {exc.message}")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ToolResult.error("Error: managed Skill content could not be read")
        if len(content) > self._MAX_CHARS:
            content = content[: self._MAX_CHARS] + "\n[Skill content truncated]"
        provenance = {
            "skillKey": skill.skill_key,
            "releaseId": skill.release_id,
            "version": skill.version,
            "sha256": skill.sha256,
            "revision": snapshot.revision,
            "snapshotId": snapshot.snapshot_id,
            "path": PurePosixPath(relative).as_posix(),
        }
        return add_provenance(content, provenance)
