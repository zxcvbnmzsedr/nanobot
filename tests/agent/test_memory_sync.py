from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.memory_sync import (
    KangarooMemoryClient,
    MemorySyncConflictError,
    MemorySyncCoordinator,
    MemorySyncError,
    RemoteMemoryBundle,
    RemoteMemoryDocument,
    RemoteMemoryHistory,
)
from nanobot.identity.credentials import KangarooCredentialStore
from nanobot.identity.principal import Principal
from nanobot.identity.runtime import TenantRuntimeStore


class FakeMemoryClient:
    def __init__(self) -> None:
        self.bundle_calls: list[str] = []
        self.append_calls: list[tuple[str, dict]] = []
        self.commit_calls: list[tuple[str, dict]] = []
        self.histories: dict[str, list[RemoteMemoryHistory]] = {}
        self.documents: dict[str, list[RemoteMemoryDocument]] = {}
        self.state_versions: dict[str, int] = {}
        self.dream_cursors: dict[str, int] = {}
        self.conflict = False
        self.conflicts_remaining = 0
        self.bundle_error = False

    async def get_bundle(self, user_scope: str) -> RemoteMemoryBundle:
        self.bundle_calls.append(user_scope)
        if self.bundle_error:
            raise MemorySyncError("bundle unavailable")
        history = self.histories.setdefault(user_scope, [])
        documents = [
            RemoteMemoryDocument(
                scopeType="system",
                documentType="memory",
                content="system fact",
                version=1,
            ),
            RemoteMemoryDocument(
                scopeType="org",
                documentType="memory",
                content="shared org fact",
                version=1,
            ),
            *self.documents.get(user_scope, []),
        ]
        return RemoteMemoryBundle(
            documents=documents,
            history=history,
            latestCursor=history[-1].cursor if history else 0,
            dreamCursor=self.dream_cursors.get(user_scope, 0),
            stateVersion=self.state_versions.get(user_scope, 0),
        )

    async def append_history(self, user_scope: str, payload: dict) -> int:
        self.append_calls.append((user_scope, payload))
        history = self.histories.setdefault(user_scope, [])
        cursor = len(history) + 1
        history.append(RemoteMemoryHistory(
            cursor=cursor,
            eventId=payload["eventId"],
            sessionKey=payload.get("sessionKey"),
            content=payload["content"],
            timestamp=datetime.fromisoformat(payload["timestamp"]),
        ))
        self.state_versions[user_scope] = self.state_versions.get(user_scope, 0) + 1
        return cursor

    async def commit_dream(self, user_scope: str, payload: dict) -> dict:
        self.commit_calls.append((user_scope, payload))
        if self.conflict:
            self.conflict = False
            raise MemorySyncConflictError("conflict")
        if self.conflicts_remaining > 0:
            self.conflicts_remaining -= 1
            raise MemorySyncConflictError("conflict")
        current = {
            document.document_type: document
            for document in self.documents.get(user_scope, [])
        }
        if self.state_versions.get(user_scope, 0) != payload["expectedStateVersion"]:
            raise MemorySyncConflictError("state version changed")
        for name, expected in payload["expectedVersions"].items():
            current_version = current[name].version if name in current else 0
            if current_version != expected:
                raise MemorySyncConflictError(f"{name} version changed")
        versions = {
            name: payload["expectedVersions"][name] + 1
            for name in payload["documents"]
        }
        current.update({
            name: RemoteMemoryDocument(
                scopeType="user",
                documentType=name,
                content=content,
                version=versions[name],
            )
            for name, content in payload["documents"].items()
        })
        self.documents[user_scope] = list(current.values())
        self.dream_cursors[user_scope] = payload["throughCursor"]
        new_state = payload["expectedStateVersion"] + 1
        self.state_versions[user_scope] = new_state
        return {
            "documentVersions": versions,
            "dreamCursor": payload["throughCursor"],
            "stateVersion": new_state,
        }


def _runtime(root: Path, user: str, org: str):
    principal = Principal(user_id=user, org_id=org)
    return TenantRuntimeStore(root).for_principal(principal)


@pytest.mark.asyncio
async def test_hydrates_system_org_and_isolated_user_mirrors(tmp_path: Path) -> None:
    client = FakeMemoryClient()
    first = _runtime(tmp_path / "tenants", "u1", "org1")
    second = _runtime(tmp_path / "tenants", "u2", "org1")
    client.documents[first.principal.user_scope] = [
        RemoteMemoryDocument(
            scopeType="user",
            documentType="memory",
            content="u1 private",
            version=3,
        )
    ]
    client.documents[second.principal.user_scope] = [
        RemoteMemoryDocument(
            scopeType="user",
            documentType="memory",
            content="u2 private",
            version=4,
        )
    ]
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]
    system_store = MemoryStore(tmp_path / "system")

    await coordinator.prepare_turn(
        identity=first.identity_metadata(),
        system_store=system_store,
        user_store=MemoryStore(first.workspace),
    )
    await coordinator.prepare_turn(
        identity=second.identity_metadata(),
        system_store=system_store,
        user_store=MemoryStore(second.workspace),
    )

    assert system_store.read_memory() == "system fact"
    assert first.org_memory.read_text() == "shared org fact"
    assert first.org_memory == second.org_memory
    assert first.user_memory.read_text() == "u1 private"
    assert second.user_memory.read_text() == "u2 private"


@pytest.mark.asyncio
async def test_local_history_outbox_flushes_on_next_turn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    client = FakeMemoryClient()
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]

    assert await coordinator.prepare_turn(
        identity=runtime.identity_metadata(),
        system_store=MemoryStore(tmp_path / "system"),
        user_store=store,
    )
    store.append_history("remember this", session_key="websocket:chat1")
    assert (store.memory_dir / ".remote_outbox.jsonl").read_text()

    assert await coordinator.prepare_turn(
        identity=runtime.identity_metadata(),
        system_store=MemoryStore(tmp_path / "system"),
        user_store=store,
    )

    assert len(client.append_calls) == 1
    assert store.read_history()[0]["content"] == "remember this"
    assert (store.memory_dir / ".remote_outbox.jsonl").read_text() == ""


@pytest.mark.asyncio
async def test_direct_memory_file_edit_commits_at_turn_boundary(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    client = FakeMemoryClient()
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]
    await coordinator.prepare_turn(
        identity=runtime.identity_metadata(),
        system_store=MemoryStore(tmp_path / "system"),
        user_store=store,
    )

    store.write_memory("remember direct edit")

    assert await coordinator.commit_changed_documents(store)
    assert client.commit_calls[-1][1]["documents"] == {
        "memory": "remember direct edit"
    }
    assert not (store.memory_dir / ".remote_documents.json").exists()


@pytest.mark.asyncio
async def test_direct_memory_conflict_rebases_and_commits(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    client = FakeMemoryClient()
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]
    identity = runtime.identity_metadata()
    system_store = MemoryStore(tmp_path / "system")
    await coordinator.prepare_turn(
        identity=identity,
        system_store=system_store,
        user_store=store,
    )
    store.write_memory("durable pending edit")
    store.append_history("pending local history", session_key="websocket:chat1")
    client.documents[runtime.principal.user_scope] = [
        RemoteMemoryDocument(
            scopeType="user",
            documentType="memory",
            content="concurrent remote edit",
            version=1,
        )
    ]
    client.state_versions[runtime.principal.user_scope] = 1

    assert await coordinator.commit_changed_documents(store)
    outbox = store.memory_dir / ".remote_documents.json"
    assert not outbox.exists()
    assert store.read_memory() == "durable pending edit"
    assert [call[1]["expectedVersions"]["memory"] for call in client.commit_calls] == [0, 1]
    assert [call[1]["expectedStateVersion"] for call in client.commit_calls] == [0, 1]
    assert store.read_history()[0]["content"] == "pending local history"
    assert (store.memory_dir / ".remote_outbox.jsonl").read_text()
    remote_memory = client.documents[runtime.principal.user_scope][0]
    assert remote_memory.content == "durable pending edit"
    assert remote_memory.version == 2


@pytest.mark.asyncio
async def test_stale_document_outbox_rebases_after_restart(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    store.write_memory("durable pending edit")
    outbox = store.memory_dir / ".remote_documents.json"
    MemorySyncCoordinator._atomic_write_text(
        outbox,
        '{"documents":{"memory":"durable pending edit"},'
        '"expectedVersions":{"memory":0},"throughCursor":0,'
        '"expectedStateVersion":0}',
    )
    client = FakeMemoryClient()
    client.documents[runtime.principal.user_scope] = [
        RemoteMemoryDocument(
            scopeType="user",
            documentType="memory",
            content="newer remote edit",
            version=1,
        )
    ]
    restored = MemorySyncCoordinator(client)  # type: ignore[arg-type]

    assert await restored.prepare_turn(
        identity=runtime.identity_metadata(),
        system_store=MemoryStore(tmp_path / "system"),
        user_store=store,
    )

    assert not outbox.exists()
    assert store.read_memory() == "durable pending edit"
    assert [call[1]["expectedVersions"]["memory"] for call in client.commit_calls] == [0, 1]


@pytest.mark.asyncio
async def test_rebased_outbox_survives_a_second_conflict(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    client = FakeMemoryClient()
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]
    identity = runtime.identity_metadata()
    system_store = MemoryStore(tmp_path / "system")
    await coordinator.prepare_turn(
        identity=identity,
        system_store=system_store,
        user_store=store,
    )
    store.write_memory("durable pending edit")
    client.conflicts_remaining = 2

    assert not await coordinator.commit_changed_documents(store)
    outbox = store.memory_dir / ".remote_documents.json"
    assert outbox.exists()
    assert store.read_memory() == "durable pending edit"

    restored = MemorySyncCoordinator(client)  # type: ignore[arg-type]
    assert await restored.prepare_turn(
        identity=identity,
        system_store=system_store,
        user_store=store,
    )

    assert not outbox.exists()
    assert store.read_memory() == "durable pending edit"


@pytest.mark.asyncio
async def test_failed_hydration_does_not_treat_existing_mirror_as_changed(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    store.write_memory("existing local mirror")
    client = FakeMemoryClient()
    client.bundle_error = True
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]

    assert not await coordinator.prepare_turn(
        identity=runtime.identity_metadata(),
        system_store=MemoryStore(tmp_path / "system"),
        user_store=store,
    )
    assert await coordinator.commit_changed_documents(store)
    assert client.commit_calls == []
    assert not (store.memory_dir / ".remote_documents.json").exists()


@pytest.mark.asyncio
async def test_cas_conflict_rehydrates_and_does_not_advance_local_cursor(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "tenants", "u1", "org1")
    store = MemoryStore(runtime.workspace)
    client = FakeMemoryClient()
    coordinator = MemorySyncCoordinator(client)  # type: ignore[arg-type]
    await coordinator.prepare_turn(
        identity=runtime.identity_metadata(),
        system_store=MemoryStore(tmp_path / "system"),
        user_store=store,
    )
    store.write_memory("local dream change")
    client.conflict = True

    committed = await coordinator.commit_dream(store, through_cursor=0)

    assert committed is False
    assert store.get_last_dream_cursor() == 0
    assert store.read_memory() == ""


@pytest.mark.asyncio
async def test_memory_client_uses_bearer_identity_and_memory_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "documents": [],
                "history": [],
                "latestCursor": 0,
                "dreamCursor": 0,
                "stateVersion": 0,
            },
        )

    credentials = KangarooCredentialStore()
    principal = Principal(user_id="u1", org_id="org1")
    credentials.put(principal, "access-token")
    client = KangarooMemoryClient(
        base_url="https://agent.example.com/nanobot/memory",
        credential_store=credentials,
        transport=httpx.MockTransport(handler),
    )

    await client.get_bundle(principal.user_scope)

    assert seen[0].url.path == "/nanobot/memory/bundle"
    assert seen[0].headers["Authorization"] == "Bearer access-token"


@pytest.mark.asyncio
async def test_memory_client_uses_management_route_for_webui_reads_and_writes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            body = {"documents": [], "userName": "Alice", "orgName": "Acme"}
        else:
            body = {
                "document": {
                    "scopeType": "user",
                    "content": "updated",
                    "version": 2,
                    "canEdit": True,
                }
            }
        return httpx.Response(200, json=body)

    credentials = KangarooCredentialStore()
    principal = Principal(user_id="u1", org_id="org1")
    credentials.put(principal, "access-token")
    client = KangarooMemoryClient(
        base_url="https://agent.example.com/nanobot/memory",
        credential_store=credentials,
        transport=httpx.MockTransport(handler),
    )

    await client.get_management(principal.user_scope)
    await client.update_management(principal.user_scope, {
        "scopeType": "user",
        "content": "updated",
        "expectedVersion": 1,
    })

    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/nanobot/memory/management"),
        ("PUT", "/nanobot/memory/management"),
    ]
    assert all(request.headers["Authorization"] == "Bearer access-token" for request in seen)
