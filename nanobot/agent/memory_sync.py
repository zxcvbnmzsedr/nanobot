"""Backend synchronization for Kangaroo-scoped local memory mirrors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from nanobot.agent.memory import MemoryStore
from nanobot.identity.credentials import (
    KangarooCredentialStore,
    KangarooIdentityError,
    get_kangaroo_credential_store,
)
from nanobot.identity.principal import IDENTITY_METADATA_KEY


class RemoteMemoryDocument(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope_type: str = Field(alias="scopeType")
    document_type: str = Field(alias="documentType")
    content: str
    version: int


class RemoteMemoryHistory(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cursor: int
    event_id: str = Field(alias="eventId")
    session_key: str | None = Field(default=None, alias="sessionKey")
    content: str
    timestamp: datetime


class RemoteMemoryBundle(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    documents: list[RemoteMemoryDocument]
    history: list[RemoteMemoryHistory]
    latest_cursor: int = Field(alias="latestCursor")
    dream_cursor: int = Field(alias="dreamCursor")
    state_version: int = Field(alias="stateVersion")


class MemorySyncError(RuntimeError):
    pass


class MemorySyncConflictError(MemorySyncError):
    pass


class MemorySyncPermissionError(MemorySyncError):
    pass


class KangarooMemoryClient:
    def __init__(
        self,
        *,
        base_url: str,
        credential_store: KangarooCredentialStore | None = None,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Kangaroo memory API URL must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.credential_store = credential_store or get_kangaroo_credential_store()
        self.timeout_s = timeout_s
        self.transport = transport

    async def get_bundle(self, user_scope: str) -> RemoteMemoryBundle:
        response = await self._request("GET", "/bundle", user_scope=user_scope)
        return RemoteMemoryBundle.model_validate(response.json())

    async def get_management(self, user_scope: str) -> dict[str, Any]:
        response = await self._request("GET", "/management", user_scope=user_scope)
        return dict(response.json())

    async def update_management(
        self,
        user_scope: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "PUT",
            "/management",
            user_scope=user_scope,
            json_body=payload,
        )
        return dict(response.json())

    async def append_history(self, user_scope: str, payload: dict[str, Any]) -> int:
        response = await self._request(
            "POST",
            "/history",
            user_scope=user_scope,
            json_body=payload,
        )
        return int(response.json()["cursor"])

    async def commit_dream(
        self,
        user_scope: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "PUT",
            "/user/dream-commit",
            user_scope=user_scope,
            json_body=payload,
        )
        return dict(response.json())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        user_scope: str,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            access_token = await self.credential_store.get_valid_access_token(user_scope)
        except KangarooIdentityError as exc:
            raise MemorySyncError("Kangaroo credential refresh failed") from exc
        if access_token is None:
            raise MemorySyncError("Kangaroo credential is unavailable")

        for attempt in range(2):
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_s,
            ) as client:
                try:
                    response = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        headers={"Authorization": f"Bearer {access_token}"},
                        json=json_body,
                    )
                except httpx.HTTPError as exc:
                    raise MemorySyncError("Kangaroo memory API request failed") from exc
            if response.status_code != 401:
                if response.status_code == 409:
                    raise MemorySyncConflictError(response.text)
                if response.status_code == 403:
                    raise MemorySyncPermissionError(response.text)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise MemorySyncError(
                        f"Kangaroo memory API returned HTTP {response.status_code}"
                    ) from exc
                return response
            if attempt > 0:
                break
            try:
                refreshed = await self.credential_store.refresh_access_token(
                    user_scope,
                    rejected_access_token=access_token,
                )
            except KangarooIdentityError as exc:
                raise MemorySyncError("Kangaroo credential refresh failed") from exc
            if refreshed is None:
                break
            access_token = refreshed
        raise MemorySyncError("Kangaroo credential was rejected")


@dataclass
class MemoryScopeBinding:
    identity: dict[str, Any]
    store: MemoryStore
    system_store: MemoryStore
    org_memory_path: Path | None
    document_versions: dict[str, int]
    document_contents: dict[str, str]
    state_version: int


class MemorySyncCoordinator:
    """Synchronize once at turn boundaries while keeping MemoryStore as the algorithm."""

    _OUTBOX_FILE = ".remote_outbox.jsonl"
    _DOCUMENT_OUTBOX_FILE = ".remote_documents.json"

    def __init__(self, client: KangarooMemoryClient) -> None:
        self.client = client
        self._bindings: dict[Path, MemoryScopeBinding] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def identity_from(
        message_metadata: Mapping[str, Any] | None,
        session_metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        for metadata in (session_metadata, message_metadata):
            identity = (
                metadata.get(IDENTITY_METADATA_KEY)
                if isinstance(metadata, Mapping)
                else None
            )
            if (
                isinstance(identity, Mapping)
                and identity.get("source") == "kangaroo"
                and isinstance(identity.get("user_scope"), str)
                and isinstance(identity.get("user_id"), str)
                and isinstance(identity.get("org_id"), str)
            ):
                return dict(identity)
        return None

    async def prepare_turn(
        self,
        *,
        identity: Mapping[str, Any],
        system_store: MemoryStore,
        user_store: MemoryStore,
    ) -> bool:
        user_scope = str(identity["user_scope"])
        lock = self._locks.setdefault(user_scope, asyncio.Lock())
        async with lock:
            org_path_value = identity.get("org_memory_path")
            binding = MemoryScopeBinding(
                identity=dict(identity),
                store=user_store,
                system_store=system_store,
                org_memory_path=(
                    Path(org_path_value)
                    if isinstance(org_path_value, str) and org_path_value
                    else None
                ),
                document_versions={},
                document_contents=self._user_documents(user_store),
                state_version=0,
            )
            self._restore_document_outbox_state(binding)
            self._bindings[user_store.workspace.resolve(strict=False)] = binding
            user_store.set_history_append_listener(
                lambda record, target=user_store: self._queue_history(target, record)
            )
            try:
                await self._flush_outbox(binding)
                await self._flush_document_outbox(binding)
                bundle = await self.client.get_bundle(user_scope)
                if bundle.latest_cursor == 0 and user_store.read_history():
                    self._queue_existing_history(user_store, user_scope)
                    await self._flush_outbox(binding)
                    bundle = await self.client.get_bundle(user_scope)
                seeded_documents = await self._seed_user_documents_if_needed(binding, bundle)
                if seeded_documents:
                    bundle = await self.client.get_bundle(user_scope)
                self._apply_bundle(binding, bundle)
                return True
            except (MemorySyncError, OSError, ValueError) as exc:
                logger.warning("Kangaroo memory sync skipped for {}: {}", user_scope, exc)
                return False

    def binding_for_store(self, store: MemoryStore) -> MemoryScopeBinding | None:
        return self._bindings.get(store.workspace.resolve(strict=False))

    def active_bindings(self) -> list[MemoryScopeBinding]:
        return list(self._bindings.values())

    def lock_for_store(self, store: MemoryStore) -> asyncio.Lock | None:
        binding = self.binding_for_store(store)
        if binding is None:
            return None
        return self._locks.setdefault(str(binding.identity["user_scope"]), asyncio.Lock())

    async def commit_dream(self, store: MemoryStore, through_cursor: int) -> bool:
        binding = self.binding_for_store(store)
        if binding is None:
            return True
        documents = {
            "memory": store.read_memory(),
            "soul": store.read_soul(),
            "user": store.read_user(),
        }
        expected = {
            document_type: binding.document_versions.get(document_type, 0)
            for document_type in documents
        }
        try:
            result = await self.client.commit_dream(
                str(binding.identity["user_scope"]),
                {
                    "documents": documents,
                    "expectedVersions": expected,
                    "throughCursor": through_cursor,
                    "expectedStateVersion": binding.state_version,
                },
            )
        except MemorySyncConflictError:
            bundle = await self.client.get_bundle(str(binding.identity["user_scope"]))
            self._apply_bundle(binding, bundle)
            return False
        except MemorySyncError as exc:
            logger.warning("Kangaroo Dream commit deferred: {}", exc)
            return False
        binding.document_versions = {
            key: int(value) for key, value in result["documentVersions"].items()
        }
        binding.document_contents = dict(documents)
        binding.state_version = int(result["stateVersion"])
        return True

    async def commit_changed_documents(self, store: MemoryStore) -> bool:
        """Persist direct tool edits to durable memory files at the turn boundary."""
        binding = self.binding_for_store(store)
        if binding is None:
            return True
        lock = self._locks.setdefault(str(binding.identity["user_scope"]), asyncio.Lock())
        async with lock:
            documents = self._user_documents(store)
            changed = {
                name: content
                for name, content in documents.items()
                if content != binding.document_contents.get(name, "")
            }
            if not changed:
                return True
            payload = {
                "documents": changed,
                "expectedVersions": {
                    name: binding.document_versions.get(name, 0) for name in changed
                },
                "throughCursor": store.get_last_dream_cursor(),
                "expectedStateVersion": binding.state_version,
            }
            self._write_document_outbox(store, payload)
            try:
                await self._commit_document_payload_with_rebase(binding, payload)
            except MemorySyncConflictError:
                logger.warning(
                    "Kangaroo direct memory commit conflicted again after rebase for {}; "
                    "local outbox retained",
                    binding.identity["user_scope"],
                )
                return False
            except MemorySyncError as exc:
                logger.warning("Kangaroo direct memory commit deferred: {}", exc)
                return False
            self._clear_document_outbox(store)
            return True

    async def _seed_user_documents_if_needed(
        self,
        binding: MemoryScopeBinding,
        bundle: RemoteMemoryBundle,
    ) -> bool:
        remote_types = {
            doc.document_type for doc in bundle.documents if doc.scope_type == "user"
        }
        documents = {
            name: content
            for name, content in {
                "memory": binding.store.read_memory(),
                "soul": binding.store.read_soul(),
                "user": binding.store.read_user(),
            }.items()
            if content and name not in remote_types
        }
        if not documents:
            return False
        await self.client.commit_dream(
            str(binding.identity["user_scope"]),
            {
                "documents": documents,
                "expectedVersions": {name: 0 for name in documents},
                "throughCursor": min(binding.store.get_last_dream_cursor(), bundle.latest_cursor),
                "expectedStateVersion": bundle.state_version,
            },
        )
        return True

    def _apply_bundle(
        self,
        binding: MemoryScopeBinding,
        bundle: RemoteMemoryBundle,
    ) -> None:
        self._apply_documents(binding, bundle)
        records = [
            {
                "cursor": item.cursor,
                "event_id": item.event_id,
                "timestamp": item.timestamp.strftime("%Y-%m-%d %H:%M"),
                "content": item.content,
                **({"session_key": item.session_key} if item.session_key else {}),
            }
            for item in bundle.history
        ]
        binding.store.replace_history_snapshot(
            records,
            latest_cursor=bundle.latest_cursor,
            dream_cursor=bundle.dream_cursor,
        )

    def _apply_documents(
        self,
        binding: MemoryScopeBinding,
        bundle: RemoteMemoryBundle,
    ) -> None:
        versions: dict[str, int] = {}
        contents: dict[str, str] = {}
        user_document_types: set[str] = set()
        for document in bundle.documents:
            if document.scope_type == "system" and document.document_type == "memory":
                self._atomic_write_text(binding.system_store.memory_file, document.content)
            elif (
                document.scope_type == "org"
                and document.document_type == "memory"
                and binding.org_memory_path is not None
            ):
                self._atomic_write_text(binding.org_memory_path, document.content)
            elif document.scope_type == "user":
                user_document_types.add(document.document_type)
                target = {
                    "memory": binding.store.memory_file,
                    "soul": binding.store.soul_file,
                    "user": binding.store.user_file,
                }.get(document.document_type)
                if target is not None:
                    self._atomic_write_text(target, document.content)
                    versions[document.document_type] = document.version
                    contents[document.document_type] = document.content
        for document_type, target in {
            "memory": binding.store.memory_file,
            "soul": binding.store.soul_file,
            "user": binding.store.user_file,
        }.items():
            if document_type not in user_document_types:
                self._atomic_write_text(target, "")
                contents[document_type] = ""
        binding.document_versions = versions
        binding.document_contents = contents
        binding.state_version = bundle.state_version

    async def _flush_outbox(self, binding: MemoryScopeBinding) -> None:
        path = binding.store.memory_dir / self._OUTBOX_FILE
        entries = self._read_jsonl(path)
        if not entries:
            return
        remaining = list(entries)
        for entry in entries:
            await self.client.append_history(str(binding.identity["user_scope"]), entry)
            remaining.pop(0)
            self._write_jsonl(path, remaining)

    async def _flush_document_outbox(self, binding: MemoryScopeBinding) -> None:
        path = binding.store.memory_dir / self._DOCUMENT_OUTBOX_FILE
        payload = self._read_json_object(path)
        if not payload:
            return
        await self._commit_document_payload_with_rebase(binding, payload)
        self._clear_document_outbox(binding.store)

    async def _commit_document_payload_with_rebase(
        self,
        binding: MemoryScopeBinding,
        payload: dict[str, Any],
    ) -> None:
        try:
            await self._commit_document_payload(binding, payload)
            return
        except MemorySyncConflictError:
            rebased = await self._rebase_document_payload(binding, payload)
        await self._commit_document_payload(binding, rebased)

    async def _rebase_document_payload(
        self,
        binding: MemoryScopeBinding,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raw_documents = payload.get("documents")
        targets = {
            "memory": binding.store.memory_file,
            "soul": binding.store.soul_file,
            "user": binding.store.user_file,
        }
        if not isinstance(raw_documents, dict) or not raw_documents:
            raise ValueError("memory document outbox has no documents")
        pending_documents: dict[str, str] = {}
        for name, content in raw_documents.items():
            if name not in targets or not isinstance(content, str):
                raise ValueError("memory document outbox contains an invalid document")
            pending_documents[str(name)] = content

        bundle = await self.client.get_bundle(str(binding.identity["user_scope"]))
        self._apply_documents(binding, bundle)
        for name, content in pending_documents.items():
            self._atomic_write_text(targets[name], content)

        rebased = {
            "documents": pending_documents,
            "expectedVersions": {
                name: binding.document_versions.get(name, 0)
                for name in pending_documents
            },
            "throughCursor": bundle.dream_cursor,
            "expectedStateVersion": bundle.state_version,
        }
        self._write_document_outbox(binding.store, rebased)
        logger.info(
            "Kangaroo memory document outbox rebased for {}",
            binding.identity["user_scope"],
        )
        return rebased

    async def _commit_document_payload(
        self,
        binding: MemoryScopeBinding,
        payload: dict[str, Any],
    ) -> None:
        result = await self.client.commit_dream(
            str(binding.identity["user_scope"]),
            payload,
        )
        documents = payload.get("documents", {})
        if isinstance(documents, dict):
            binding.document_contents.update(
                {str(name): str(content) for name, content in documents.items()}
            )
        binding.document_versions.update(
            {key: int(value) for key, value in result["documentVersions"].items()}
        )
        binding.state_version = int(result["stateVersion"])

    @staticmethod
    def _user_documents(store: MemoryStore) -> dict[str, str]:
        return {
            "memory": store.read_memory(),
            "soul": store.read_soul(),
            "user": store.read_user(),
        }

    def _write_document_outbox(
        self,
        store: MemoryStore,
        payload: dict[str, Any],
    ) -> None:
        self._atomic_write_text(
            store.memory_dir / self._DOCUMENT_OUTBOX_FILE,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        )

    def _restore_document_outbox_state(self, binding: MemoryScopeBinding) -> None:
        payload = self._read_json_object(
            binding.store.memory_dir / self._DOCUMENT_OUTBOX_FILE
        )
        if not payload:
            return
        expected = payload.get("expectedVersions")
        if isinstance(expected, dict):
            binding.document_versions = {
                str(name): int(version) for name, version in expected.items()
            }
        state_version = payload.get("expectedStateVersion")
        if isinstance(state_version, int):
            binding.state_version = state_version
        binding.document_contents = self._user_documents(binding.store)

    def _clear_document_outbox(self, store: MemoryStore) -> None:
        (store.memory_dir / self._DOCUMENT_OUTBOX_FILE).unlink(missing_ok=True)

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _queue_history(self, store: MemoryStore, record: dict[str, Any]) -> None:
        payload = {
            "eventId": str(record["event_id"]),
            "sessionKey": record.get("session_key"),
            "content": str(record["content"]),
            "timestamp": str(record["timestamp"]),
        }
        path = store.memory_dir / self._OUTBOX_FILE
        entries = self._read_jsonl(path)
        if any(entry.get("eventId") == payload["eventId"] for entry in entries):
            return
        entries.append(payload)
        self._write_jsonl(path, entries)

    def _queue_existing_history(self, store: MemoryStore, user_scope: str) -> None:
        for entry in store.read_history():
            if not entry.get("event_id"):
                source = json.dumps(entry, ensure_ascii=True, sort_keys=True)
                entry["event_id"] = hashlib.sha256(
                    f"{user_scope}:{source}".encode("utf-8")
                ).hexdigest()
            self._queue_history(store, entry)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        with suppress(FileNotFoundError):
            for line in path.read_text(encoding="utf-8").splitlines():
                with suppress(json.JSONDecodeError):
                    value = json.loads(line)
                    if isinstance(value, dict):
                        entries.append(value)
        return entries

    @classmethod
    def _write_jsonl(cls, path: Path, entries: list[dict[str, Any]]) -> None:
        content = "".join(
            json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n"
            for entry in entries
        )
        cls._atomic_write_text(path, content)

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            with suppress(PermissionError):
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
