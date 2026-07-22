from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.read_skill import ReadSkillTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.identity.principal import IDENTITY_METADATA_KEY, Principal
from nanobot.identity.runtime import TenantRuntimeStore
from nanobot.session.manager import Session
from nanobot.skill_market.models import InstalledSkill
from nanobot.skill_market.package import StagedRelease
from nanobot.skill_market.provenance import parse_provenance
from nanobot.skill_market.store import (
    SKILL_SNAPSHOT_METADATA_KEY,
    ManagedSkillStore,
    pin_snapshot_in_metadata,
)


def _activate(
    store: ManagedSkillStore,
    *,
    version: str,
    digest: str,
    description: str,
):
    relative = store.release_relative_path("managed-guide", version, digest)
    skill = InstalledSkill(
        skillKey="managed-guide",
        releaseId=version,
        version=version,
        sha256=digest,
        relativePath=relative,
        signingKeyId="key",
    )
    staged = store.staging / f"stage-{version}"
    staged.mkdir(parents=True)
    content = (
        f"---\nname: managed-guide\ndescription: {description}\n---\n\n# {description}"
    ).encode("utf-8")
    (staged / "SKILL.md").write_bytes(content)
    (staged / ".release.json").write_text(
        json.dumps(
            {
                "releaseId": version,
                "skillKey": "managed-guide",
                "version": version,
                "sha256": digest,
                "signingKeyId": "key",
                "artifactManifest": {
                    "schemaVersion": 1,
                    "skillKey": "managed-guide",
                    "version": version,
                    "minRuntimeVersion": None,
                    "files": [
                        {
                            "path": "SKILL.md",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    store.install_staged_release(
        StagedRelease(path=staged, manifest=None, signature=""),  # type: ignore[arg-type]
        skill,
    )
    revision = f"g{version[0]}-o{version[0]}:" + digest
    return store.activate(revision, {"managed-guide": skill})


@pytest.mark.asyncio
async def test_read_skill_uses_turn_pinned_snapshot_and_subagent_inherits_it(tmp_path: Path) -> None:
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(
        Principal(user_id="user-1", org_id="org-1")
    )
    store = ManagedSkillStore(runtime.managed_skills)
    first = _activate(store, version="1.0.0", digest="a" * 64, description="Old guide")
    metadata = pin_snapshot_in_metadata(
        {IDENTITY_METADATA_KEY: runtime.identity_metadata()}
    )
    _activate(store, version="2.0.0", digest="b" * 64, description="New guide")

    tool = ReadSkillTool()
    with request_context(RequestContext(channel="test", chat_id="1", metadata=metadata)):
        result = await tool.execute("managed-guide")

    assert "Old guide" in result
    assert "New guide" not in result
    assert parse_provenance(result)["snapshotId"] == first.snapshot_id

    manager = SubagentManager(
        workspace=runtime.workspace,
        bus=MessageBus(),
        max_tool_result_chars=4096,
    )
    prompt = manager._build_subagent_prompt(
        workspace=runtime.workspace,
        request_metadata=metadata,
    )
    assert "Old guide" in prompt
    assert "New guide" not in prompt


def test_context_uses_tenant_workspace_and_managed_snapshot_without_path_disclosure(
    tmp_path: Path,
) -> None:
    system_workspace = tmp_path / "system"
    system_workspace.mkdir()
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(
        Principal(user_id="user-1", org_id="org-1")
    )
    (runtime.workspace / "skills" / "local-guide").mkdir(parents=True)
    (runtime.workspace / "skills" / "local-guide" / "SKILL.md").write_text(
        "---\nname: local-guide\ndescription: Tenant local\n---\n\n# Local",
        encoding="utf-8",
    )
    store = ManagedSkillStore(runtime.managed_skills)
    _activate(store, version="1.0.0", digest="a" * 64, description="Managed tenant guide")

    prompt = ContextBuilder(system_workspace).build_system_prompt(
        workspace=runtime.workspace,
        session_metadata={IDENTITY_METADATA_KEY: runtime.identity_metadata()},
    )

    assert "Tenant local" in prompt
    assert "Managed tenant guide" in prompt
    assert 'read_skill(skill_name="managed-guide")' in prompt
    assert str(runtime.managed_skills) not in prompt


def test_managed_and_workspace_name_collision_fails_closed(tmp_path: Path) -> None:
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(
        Principal(user_id="user-1", org_id="org-1")
    )
    collision = runtime.workspace / "skills" / "managed-guide"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: managed-guide\ndescription: Shadow\n---\n\n# Shadow",
        encoding="utf-8",
    )
    store = ManagedSkillStore(runtime.managed_skills)
    _activate(store, version="1.0.0", digest="a" * 64, description="Managed")
    builder = ContextBuilder(tmp_path / "system")
    loader = builder.skills_for_workspace(
        runtime.workspace,
        session_metadata={IDENTITY_METADATA_KEY: runtime.identity_metadata()},
    )

    names = {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}
    assert "managed-guide" not in names
    assert loader.collisions == {"managed-guide"}


def test_read_skill_body_is_not_persisted_in_session_or_checkpoint(tmp_path: Path) -> None:
    content = (
        '<!-- nanobot-skill-provenance:{"sha256":"' + "a" * 64 + '"} -->\n'
        "TOP SECRET SKILL BODY"
    )
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_skill", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "read_skill", "content": content},
    ]
    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = 16_000
    loop.sessions = MagicMock()
    session = Session(key="test:managed")

    loop._save_turn(session, messages, skip=0)
    loop._set_runtime_checkpoint(
        session,
        {"completed_tool_results": [messages[1]]},
    )

    persisted = session.messages[1]["content"]
    checkpoint = session.metadata[loop._RUNTIME_CHECKPOINT_KEY]["completed_tool_results"][0][
        "content"
    ]
    assert "TOP SECRET" not in persisted
    assert "TOP SECRET" not in checkpoint
    assert parse_provenance(persisted) == {"sha256": "a" * 64}


def test_revoked_skill_result_body_is_removed_from_replay(tmp_path: Path) -> None:
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(
        Principal(user_id="user-1", org_id="org-1")
    )
    store = ManagedSkillStore(runtime.managed_skills)
    _activate(store, version="1.0.0", digest="a" * 64, description="Managed")
    store.add_revoked_digests({"a" * 64})
    content = (
        '<!-- nanobot-skill-provenance:{"sha256":"' + "a" * 64 + '"} -->\n'
        "REVOKED BODY"
    )
    history = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "name": "read_skill", "content": content},
    ]

    messages = ContextBuilder(tmp_path / "system").build_messages(
        history,
        "continue",
        workspace=runtime.workspace,
        session_metadata={IDENTITY_METADATA_KEY: runtime.identity_metadata()},
    )

    assert "REVOKED BODY" not in messages[2]["content"]
    assert "has been revoked" in messages[2]["content"]


@pytest.mark.asyncio
async def test_read_skill_returns_controlled_error_for_invalid_revocation_state(
    tmp_path: Path,
) -> None:
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(
        Principal(user_id="user-1", org_id="org-1")
    )
    store = ManagedSkillStore(runtime.managed_skills)
    snapshot = _activate(store, version="1.0.0", digest="a" * 64, description="Secret")
    store.revoked_path.write_text("{broken", encoding="utf-8")
    metadata = {
        IDENTITY_METADATA_KEY: runtime.identity_metadata(),
        SKILL_SNAPSHOT_METADATA_KEY: snapshot.model_dump(mode="json", by_alias=True),
    }

    with request_context(RequestContext(channel="test", chat_id="1", metadata=metadata)):
        result = await ReadSkillTool().execute("managed-guide")

    assert result.is_error is True
    assert result == "Error: Managed Skill revocation state is invalid"
    assert "Secret" not in result


def test_invalid_revocation_state_removes_read_skill_body_from_replay(
    tmp_path: Path,
) -> None:
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(
        Principal(user_id="user-1", org_id="org-1")
    )
    store = ManagedSkillStore(runtime.managed_skills)
    store.revoked_path.write_bytes(b"\xff")
    content = (
        '<!-- nanobot-skill-provenance:{"sha256":"' + "a" * 64 + '"} -->\n'
        "UNVERIFIED SKILL BODY"
    )
    history = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "name": "read_skill", "content": content},
    ]

    messages = ContextBuilder(tmp_path / "system").build_messages(
        history,
        "continue",
        workspace=runtime.workspace,
        session_metadata={IDENTITY_METADATA_KEY: runtime.identity_metadata()},
    )

    assert "UNVERIFIED SKILL BODY" not in messages[2]["content"]
    assert "could not be verified" in messages[2]["content"]


def test_request_metadata_preserves_trusted_session_runtime_paths(tmp_path: Path) -> None:
    principal = Principal(user_id="user-1", org_id="org-1")
    runtime = TenantRuntimeStore(tmp_path / "runtime").for_principal(principal)
    session = Session(key="websocket:chat-1")
    session.metadata[IDENTITY_METADATA_KEY] = runtime.identity_metadata()
    message_identity = {
        **principal.metadata(),
        "managed_skills_path": str(tmp_path / "forged"),
    }
    message = InboundMessage(
        channel="websocket",
        sender_id="user-1",
        chat_id="chat-1",
        content="hello",
        metadata={IDENTITY_METADATA_KEY: message_identity},
    )

    metadata = AgentLoop._request_metadata(message, session)

    assert (
        metadata[IDENTITY_METADATA_KEY]["managed_skills_path"]
        == str(runtime.managed_skills)
    )
