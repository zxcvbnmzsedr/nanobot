"""High-level self-service and desired-state synchronization for managed Skills."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import random
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import ValidationError

from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.identity.principal import Principal
from nanobot.skill_market.client import CredentialStore, SkillMarketClient
from nanobot.skill_market.errors import SkillArtifactError, SkillMarketError
from nanobot.skill_market.models import (
    DesiredManifest,
    InstalledSkill,
    SkillOperationResult,
    SkillSnapshot,
    SkillSyncReport,
    SyncResult,
)
from nanobot.skill_market.package import validate_and_stage_archive
from nanobot.skill_market.settings import SkillMarketSettings
from nanobot.skill_market.signing import canonical_json, verify_signed_json
from nanobot.skill_market.store import ManagedSkillStore

SkillEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
_UPDATE_POLICIES = frozenset({"manual", "notify", "auto_stable", "pinned"})
_SKILL_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REVISION = re.compile(r"^g(?P<global>\d+)-o(?P<org>\d+):(?P<digest>[0-9a-f]{64})$")
_ENTITY_TAG = re.compile(r'^(?:W/)?"(?P<opaque>[\x21\x23-\x7e\x80-\xff]*)"$')
_FULL_MANIFEST_REFRESH_S = 12 * 60 * 60


class SkillMarketService:
    """Pull signed desired state and expose account-authorized marketplace operations."""

    def __init__(
        self,
        settings: SkillMarketSettings,
        credential_store: CredentialStore,
        *,
        http_client: httpx.AsyncClient | None = None,
        builtin_skills_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.client = SkillMarketClient(settings, credential_store, http_client=http_client)
        self.builtin_skills_dir = (builtin_skills_dir or BUILTIN_SKILLS_DIR).resolve(strict=False)
        self._callbacks: set[SkillEventCallback] = set()
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._poll_principals: dict[str, Principal] = {}
        self._report_tasks: set[asyncio.Task[None]] = set()

    def _store(self, principal: Principal) -> ManagedSkillStore:
        root = (
            self.settings.runtime_root
            / "organizations"
            / principal.org_scope
            / "managed-skills"
        )
        return ManagedSkillStore(root)

    @staticmethod
    def _validate_skill_key(skill_key: str) -> str:
        if not _SKILL_KEY.fullmatch(skill_key):
            raise SkillMarketError("SKILL_KEY_INVALID", "Skill key is invalid")
        return skill_key

    @staticmethod
    def _validate_policy(update_policy: str) -> str:
        if update_policy not in _UPDATE_POLICIES:
            raise SkillMarketError("UPDATE_POLICY_INVALID", "Skill update policy is invalid")
        return update_policy

    def _reserved_names(self) -> set[str]:
        try:
            return {
                path.name
                for path in self.builtin_skills_dir.iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            }
        except OSError:
            return set()

    def subscribe(self, callback: SkillEventCallback) -> Callable[[], None]:
        """Subscribe to activated snapshots; returns an idempotent unsubscribe function."""
        self._callbacks.add(callback)

        def unsubscribe() -> None:
            self._callbacks.discard(callback)

        return unsubscribe

    async def _publish(self, event: dict[str, Any]) -> None:
        for callback in tuple(self._callbacks):
            try:
                result = callback(dict(event))
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Skill marketplace event callback failed")

    async def catalog(
        self,
        principal: Principal,
        *,
        query: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        if page < 1 or not 1 <= page_size <= 100:
            raise SkillMarketError("PAGINATION_INVALID", "Skill marketplace pagination is invalid")
        return await self.client.catalog(
            principal,
            query_text=query,
            category=category,
            page=page,
            page_size=page_size,
        )

    async def detail(self, principal: Principal, skill_key: str) -> dict[str, Any]:
        return await self.client.detail(principal, self._validate_skill_key(skill_key))

    async def inventory(self, principal: Principal) -> dict[str, Any]:
        remote_available = True
        remote_error_code: str | None = None
        try:
            remote = await self.client.subscriptions(principal)
        except SkillMarketError as exc:
            remote = {}
            remote_available = False
            remote_error_code = exc.code
        store = self._store(principal)
        snapshot = store.current_snapshot()
        state = store.sync_state()
        valid_until = state.get("manifestValidUntil")
        manifest_expired = not self._is_future_timestamp(valid_until)
        lkg_stale = not self._is_recent_timestamp(
            state.get("lastSuccessAt"),
            max_age_s=self.settings.max_stale_s,
        )
        installed = [] if snapshot is None else [
            value.model_dump(mode="json", by_alias=True)
            for _, value in sorted(snapshot.skills.items())
        ]
        return {
            **remote,
            "remoteAvailable": remote_available,
            "remoteErrorCode": remote_error_code,
            "local": {
                "revision": snapshot.revision if snapshot else None,
                "snapshotId": snapshot.snapshot_id if snapshot else None,
                "installed": installed,
                "sync": {
                    **state,
                    "manifestExpired": manifest_expired,
                    "lkgStale": lkg_stale,
                    "lkgFresh": not lkg_stale,
                    "staleSince": state.get("lastSuccessAt") if lkg_stale else None,
                },
            },
        }

    def active_entries(self, principal: Principal) -> list[dict[str, Any]]:
        """Return the local active managed entries without contacting the control plane."""
        snapshot = self._store(principal).current_snapshot()
        if snapshot is None:
            return []
        return [
            {
                **skill.model_dump(mode="json", by_alias=True),
                "snapshotId": snapshot.snapshot_id,
                "revision": snapshot.revision,
            }
            for _, skill in sorted(snapshot.skills.items())
        ]

    async def install(
        self,
        principal: Principal,
        skill_key: str,
        *,
        version: str | None = None,
        update_policy: str = "manual",
        expected_row_version: str | int | None = None,
    ) -> SkillOperationResult:
        return await self._put_and_sync(
            principal,
            skill_key,
            version=version,
            update_policy=update_policy,
            expected_row_version=expected_row_version,
            reason="install",
        )

    async def update(
        self,
        principal: Principal,
        skill_key: str,
        *,
        version: str | None = None,
        update_policy: str = "manual",
        expected_row_version: str | int | None = None,
    ) -> SkillOperationResult:
        return await self._put_and_sync(
            principal,
            skill_key,
            version=version,
            update_policy=update_policy,
            expected_row_version=expected_row_version,
            reason="update",
        )

    async def rollback(
        self,
        principal: Principal,
        skill_key: str,
        *,
        version: str,
        expected_row_version: str | int | None = None,
    ) -> SkillOperationResult:
        if not version.strip():
            raise SkillMarketError("VERSION_INVALID", "Rollback version is required")
        return await self._put_and_sync(
            principal,
            skill_key,
            version=version,
            update_policy="pinned",
            expected_row_version=expected_row_version,
            reason="rollback",
        )

    async def uninstall(
        self,
        principal: Principal,
        skill_key: str,
        *,
        expected_row_version: str | int | None = None,
    ) -> SkillOperationResult:
        key = self._validate_skill_key(skill_key)
        remote = await self.client.delete_subscription(
            principal,
            key,
            expected_row_version=expected_row_version,
        )
        return await self._operation_sync(principal, key, remote, reason="uninstall")

    async def set_policy(
        self,
        principal: Principal,
        skill_key: str,
        *,
        update_policy: str,
        version: str | None = None,
        expected_row_version: str | int | None = None,
    ) -> SkillOperationResult:
        key = self._validate_skill_key(skill_key)
        policy = self._validate_policy(update_policy)
        if policy == "auto_stable":
            version = None
        elif version is None:
            snapshot = self._store(principal).current_snapshot()
            installed = snapshot.skills.get(key) if snapshot is not None else None
            if installed is None:
                raise SkillMarketError(
                    "POLICY_VERSION_REQUIRED",
                    "Update policy requires a version for a Skill that is not installed",
                )
            version = installed.version
        return await self._put_and_sync(
            principal,
            key,
            version=version,
            update_policy=policy,
            expected_row_version=expected_row_version,
            reason="policy",
        )

    async def _put_and_sync(
        self,
        principal: Principal,
        skill_key: str,
        *,
        version: str | None,
        update_policy: str,
        expected_row_version: str | int | None,
        reason: str,
    ) -> SkillOperationResult:
        key = self._validate_skill_key(skill_key)
        policy = self._validate_policy(update_policy)
        body: dict[str, Any] = {"updatePolicy": policy}
        if version is not None:
            if not version.strip():
                raise SkillMarketError("VERSION_INVALID", "Skill version is invalid")
            body["version"] = version
        if expected_row_version is not None:
            body["expectedRowVersion"] = expected_row_version
        remote = await self.client.put_subscription(principal, key, body)
        return await self._operation_sync(principal, key, remote, reason=reason)

    async def _operation_sync(
        self,
        principal: Principal,
        skill_key: str,
        remote: dict[str, Any],
        *,
        reason: str,
    ) -> SkillOperationResult:
        result = await self._sync_now(principal, force=True, reason=reason)
        if result.status != "failed" and not result.changed_skills:
            await self._publish(
                {
                    "type": "skills_updated",
                    "orgScope": principal.org_scope,
                    "revision": result.revision,
                    "snapshotId": result.snapshot_id,
                    "reason": reason,
                    "changed": [],
                    "skillKey": skill_key,
                }
            )
        status = "failed" if result.status == "failed" else "applied"
        return SkillOperationResult(
            status=status,
            skill_key=skill_key,
            desired_revision=result.revision,
            snapshot_id=result.snapshot_id,
            error_code=result.error_code,
            details={"subscription": remote},
        )

    async def sync_now(self, principal: Principal, *, force: bool = False) -> SyncResult:
        return await self._sync_now(principal, force=force, reason="sync")

    async def _sync_now(
        self,
        principal: Principal,
        *,
        force: bool,
        reason: str,
    ) -> SyncResult:
        if not self.settings.enabled:
            raise SkillMarketError("SKILL_MARKET_DISABLED", "Skill marketplace is disabled")
        lock = self._sync_locks.setdefault(principal.org_scope, asyncio.Lock())
        async with lock:
            store = self._store(principal)
            state = store.sync_state()
            previous = store.current_snapshot()
            correlation_id = uuid.uuid4().hex
            desired_revision = ""
            try:
                response = await self.client.manifest(
                    principal,
                    etag=self._manifest_etag(state, previous=previous, force=force),
                )
                if response.status_code == 304 and previous is None:
                    response = await self.client.manifest(principal, etag=None)
                    if response.status_code == 304:
                        raise SkillMarketError(
                            "CONTROL_PLANE_INVALID_RESPONSE",
                            "Skill control plane returned 304 without a recoverable local snapshot",
                            http_status=502,
                            retryable=True,
                        )
                if response.status_code == 304:
                    poll = self._poll_interval(state.get("pollAfterSeconds"))
                    now = datetime.now(timezone.utc).isoformat()
                    desired_revision = str(state.get("revision") or previous.revision)
                    store.update_sync_state(
                        lastCheckedAt=now,
                        lastSuccessAt=now,
                        lastStatus="unchanged",
                        lastErrorCode=None,
                    )
                    self._schedule_sync_report(
                        principal,
                        reason=reason,
                        status="unchanged",
                        previous=previous,
                        current=previous,
                        desired_revision=desired_revision,
                        correlation_id=correlation_id,
                    )
                    return SyncResult(
                        status="unchanged",
                        revision=previous.revision,
                        snapshot_id=previous.snapshot_id,
                        poll_after_seconds=poll,
                    )
                if response.payload is None:
                    raise SkillMarketError(
                        "CONTROL_PLANE_INVALID_RESPONSE",
                        "Skill manifest response is empty",
                        http_status=502,
                        retryable=True,
                    )
                raw = response.payload
                raw_revision = raw.get("revision")
                if isinstance(raw_revision, str):
                    desired_revision = raw_revision
                verify_signed_json(
                    raw,
                    self.settings.public_keys,
                    required=self.settings.require_signatures,
                )
                try:
                    manifest = DesiredManifest.model_validate(raw)
                except ValidationError as exc:
                    raise SkillMarketError(
                        "MANIFEST_INVALID",
                        "Skill manifest is invalid",
                        http_status=422,
                    ) from exc
                self._validate_manifest(principal, manifest, response.etag, state)
                snapshot, changed = await self._apply_manifest(store, principal, manifest)
                poll = self._poll_interval(manifest.poll_after_seconds)
                now = datetime.now(timezone.utc).isoformat()
                store.update_sync_state(
                    etag=response.etag,
                    globalRevision=manifest.generation.global_revision,
                    orgRevision=manifest.generation.org_revision,
                    revision=manifest.revision,
                    manifestValidUntil=manifest.valid_until.isoformat(),
                    pollAfterSeconds=poll,
                    lastFullFetchAt=now,
                    lastCheckedAt=now,
                    lastSuccessAt=now,
                    lastStatus="applied" if changed else "unchanged",
                    lastErrorCode=None,
                )
                if changed:
                    await self._publish(
                        {
                            "type": "skills_updated",
                            "orgScope": principal.org_scope,
                            "revision": snapshot.revision,
                            "snapshotId": snapshot.snapshot_id,
                            "previousSnapshotId": previous.snapshot_id if previous else None,
                            "reason": reason,
                            "changed": changed,
                        }
                    )
                self._schedule_sync_report(
                    principal,
                    reason=reason,
                    status="applied" if changed else "unchanged",
                    previous=previous,
                    current=snapshot,
                    changed=changed,
                    desired_revision=manifest.revision,
                    correlation_id=correlation_id,
                )
                return SyncResult(
                    status="applied" if changed else "unchanged",
                    revision=snapshot.revision,
                    snapshot_id=snapshot.snapshot_id,
                    poll_after_seconds=poll,
                    changed_skills=changed,
                )
            except SkillMarketError as exc:
                poll = self._poll_interval(state.get("pollAfterSeconds"))
                store.update_sync_state(
                    lastCheckedAt=datetime.now(timezone.utc).isoformat(),
                    lastStatus="failed",
                    lastErrorCode=exc.code,
                )
                logger.warning("Managed Skill sync rejected for org {}: {}", principal.org_scope, exc.code)
                self._schedule_sync_report(
                    principal,
                    reason=reason,
                    status="rejected",
                    previous=previous,
                    current=previous,
                    error_code=exc.code,
                    error_message=exc.message,
                    desired_revision=desired_revision,
                    correlation_id=correlation_id,
                )
                return SyncResult(
                    status="failed",
                    revision=previous.revision if previous else None,
                    snapshot_id=previous.snapshot_id if previous else None,
                    poll_after_seconds=poll,
                    error_code=exc.code,
                )

    def _manifest_etag(
        self,
        state: Mapping[str, Any],
        *,
        previous: SkillSnapshot | None,
        force: bool,
    ) -> str | None:
        if force or previous is None:
            return None
        etag = state.get("etag")
        if not isinstance(etag, str) or not etag:
            return None
        poll = self._poll_interval(state.get("pollAfterSeconds"))
        valid_until = self._parse_timestamp(state.get("manifestValidUntil"))
        now = datetime.now(timezone.utc)
        if valid_until is None or valid_until <= now.timestamp() + poll:
            return None
        last_full_fetch = self._parse_timestamp(state.get("lastFullFetchAt"))
        if last_full_fetch is None or now.timestamp() - last_full_fetch >= _FULL_MANIFEST_REFRESH_S:
            return None
        return etag

    def _validate_manifest(
        self,
        principal: Principal,
        manifest: DesiredManifest,
        etag: str | None,
        state: Mapping[str, Any],
    ) -> None:
        if manifest.audience != f"org:{principal.org_id}":
            raise SkillMarketError(
                "MANIFEST_AUDIENCE_MISMATCH",
                "Skill manifest belongs to another organization",
                http_status=403,
            )
        if manifest.is_expired():
            raise SkillMarketError("MANIFEST_EXPIRED", "Skill manifest has expired", http_status=422)
        if etag is not None and self._etag_opaque_value(etag) != manifest.revision:
            raise SkillMarketError("MANIFEST_ETAG_MISMATCH", "Skill manifest ETag is inconsistent")
        global_revision, org_revision, revision_digest = self._revision_parts(manifest.revision)
        if (
            global_revision != manifest.generation.global_revision
            or org_revision != manifest.generation.org_revision
        ):
            raise SkillMarketError(
                "MANIFEST_INVALID",
                "Skill manifest revision does not match its generation",
            )
        desired_payload = {
            "audience": manifest.audience,
            "generation": manifest.generation.model_dump(mode="json", by_alias=True),
            "entries": [
                entry.model_dump(mode="json", by_alias=True)
                for entry in manifest.entries
            ],
            "revocations": [
                revocation.model_dump(mode="json", by_alias=True)
                for revocation in manifest.revocations
            ],
        }
        expected_digest = hashlib.sha256(canonical_json(desired_payload)).hexdigest()
        if revision_digest != expected_digest:
            raise SkillMarketError(
                "MANIFEST_INVALID",
                "Skill manifest revision digest does not match desired state",
            )
        previous_global = state.get("globalRevision")
        previous_org = state.get("orgRevision")
        previous_revision = state.get("revision")
        if isinstance(previous_global, int) and isinstance(previous_org, int):
            if global_revision < previous_global or org_revision < previous_org:
                raise SkillMarketError("MANIFEST_REPLAYED", "Older Skill manifest was rejected")
            if (
                global_revision == previous_global
                and org_revision == previous_org
                and manifest.revision != previous_revision
            ):
                raise SkillMarketError(
                    "REVISION_CONFLICT",
                    "Skill manifest generation was reused",
                    http_status=409,
                )

    @staticmethod
    def _etag_opaque_value(etag: str) -> str | None:
        match = _ENTITY_TAG.fullmatch(etag)
        return match.group("opaque") if match is not None else None

    @staticmethod
    def _revision_parts(revision: str) -> tuple[int, int, str]:
        match = _REVISION.fullmatch(revision)
        if match is None:
            raise SkillMarketError("MANIFEST_INVALID", "Skill manifest revision is invalid")
        return (
            int(match.group("global")),
            int(match.group("org")),
            match.group("digest"),
        )

    async def _apply_manifest(
        self,
        store: ManagedSkillStore,
        principal: Principal,
        manifest: DesiredManifest,
    ) -> tuple[SkillSnapshot, list[str]]:
        previous = store.current_snapshot()
        previous_skills = previous.skills if previous is not None else {}
        target: dict[str, InstalledSkill] = {}
        revoked = {item.sha256 for item in manifest.revocations}
        # Signed release revocations are emergency state. Persist them before any
        # artifact I/O so a failed update cannot leave a pinned old turn executable.
        store.add_revoked_digests(revoked)
        installs: list[tuple[Any, InstalledSkill]] = []
        for entry in manifest.entries:
            if entry.action in {"disable", "remove"}:
                continue
            if entry.sha256 in revoked:
                raise SkillMarketError(
                    "MANIFEST_INVALID",
                    "A revoked Skill release cannot be installed",
                )
            relative = store.release_relative_path(
                entry.skill_key,
                entry.release_id,
                entry.sha256,
            )
            installed = InstalledSkill(
                skill_key=entry.skill_key,
                release_id=entry.release_id,
                version=entry.version,
                sha256=entry.sha256,
                relative_path=relative,
                mandatory=entry.mandatory,
                signing_key_id=entry.signing_key_id,
            )
            target[entry.skill_key] = installed
            if not store.has_release(installed):
                installs.append((entry, installed))

        reserved = self._reserved_names()
        for entry, installed in installs:
            artifact_response = await self.client.artifact(principal, entry.artifact_path)
            actual_digest = hashlib.sha256(artifact_response.content).hexdigest()
            try:
                staged = await asyncio.to_thread(
                    validate_and_stage_archive,
                    artifact_response.content,
                    entry,
                    public_keys=self.settings.public_keys,
                    staging_root=store.staging,
                    limits=self.settings.limits,
                    runtime_version=self.settings.runtime_version,
                    response_headers=artifact_response.headers,
                    reserved_skill_names=reserved,
                )
                await asyncio.to_thread(store.install_staged_release, staged, installed)
            except SkillArtifactError as exc:
                await asyncio.to_thread(
                    store.quarantine_artifact,
                    artifact_response.content,
                    digest=actual_digest,
                    code=exc.code,
                    message=exc.message,
                )
                raise

        snapshot = await asyncio.to_thread(store.activate, manifest.revision, target)
        changed = sorted(
            key
            for key in set(previous_skills) | set(target)
            if previous_skills.get(key) != target.get(key)
        )
        return snapshot, changed

    def _poll_interval(self, value: Any) -> int:
        try:
            interval = int(value)
        except (TypeError, ValueError):
            interval = self.settings.default_poll_s
        return max(self.settings.min_poll_s, min(self.settings.max_poll_s, interval))

    def _schedule_sync_report(
        self,
        principal: Principal,
        *,
        reason: str,
        status: str,
        previous: SkillSnapshot | None,
        current: SkillSnapshot | None,
        changed: list[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        desired_revision: str,
        correlation_id: str,
    ) -> None:
        previous_skills = previous.skills if previous is not None else {}
        current_skills = current.skills if current is not None else {}
        rejected = status == "rejected"
        event_type = "reject" if rejected else {
            "install": "install",
            "update": "update",
            "uninstall": "remove",
            "rollback": "rollback",
            "policy": "update",
        }.get(reason, "sync")
        names: list[str | None] = sorted(set(changed or ())) or [None]
        for name in names:
            old = previous_skills.get(name) if name is not None else None
            new = current_skills.get(name) if name is not None else None
            release_id = (new or old).release_id if (new or old) is not None else None
            if release_id is not None and (
                not release_id.isdigit() or release_id.startswith("0")
            ):
                release_id = None
            report = SkillSyncReport(
                eventId=uuid.uuid4().hex,
                correlationId=correlation_id,
                eventType=event_type,
                desiredRevision=desired_revision,
                appliedRevision=current.revision if current is not None else "",
                skillKey=name,
                releaseId=release_id,
                oldDigest=old.sha256 if old is not None else "",
                newDigest=new.sha256 if new is not None else "",
                signingKeyId=(new or old).signing_key_id if (new or old) is not None else "",
                status="rejected" if rejected else "success",
                errorCode=error_code or "",
                rejectionReason=(error_message or "")[:500],
                runtimeVersion=self.settings.runtime_version,
            )
            payload = report.model_dump(mode="json", by_alias=True)
            task = asyncio.create_task(self._send_sync_report(principal, payload))
            self._report_tasks.add(task)
            task.add_done_callback(self._report_tasks.discard)

    async def _send_sync_report(
        self,
        principal: Principal,
        payload: Mapping[str, Any],
    ) -> None:
        try:
            await self.client.sync_report(principal, payload)
        except Exception:
            logger.warning(
                "Managed Skill sync report could not be delivered for event {}",
                payload.get("eventId"),
            )

    @staticmethod
    def _parse_timestamp(value: Any) -> float | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return parsed.timestamp()
        except (OSError, OverflowError, ValueError):
            return None

    @classmethod
    def _is_future_timestamp(cls, value: Any) -> bool:
        timestamp = cls._parse_timestamp(value)
        return timestamp is not None and timestamp > datetime.now(timezone.utc).timestamp()

    @classmethod
    def _is_recent_timestamp(cls, value: Any, *, max_age_s: int) -> bool:
        timestamp = cls._parse_timestamp(value)
        if timestamp is None:
            return False
        age = datetime.now(timezone.utc).timestamp() - timestamp
        return 0 <= age <= max_age_s

    async def start(self, principal: Principal) -> None:
        """Start one idempotent background pull loop for an organization."""
        if not self.settings.enabled:
            return
        self._poll_principals[principal.org_scope] = principal
        current = self._poll_tasks.get(principal.org_scope)
        if current is not None and not current.done():
            return
        self._poll_tasks[principal.org_scope] = asyncio.create_task(
            self._poll_loop(principal.org_scope),
            name=f"skill-sync-{principal.org_scope}",
        )

    async def _poll_loop(self, org_scope: str) -> None:
        while True:
            principal = self._poll_principals.get(org_scope)
            if principal is None:
                return
            try:
                result = await self.sync_now(principal)
                interval = result.poll_after_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Managed Skill background sync failed for org {}", org_scope)
                interval = self.settings.default_poll_s
            delay = interval * random.uniform(0.9, 1.1)
            await asyncio.sleep(max(self.settings.min_poll_s, delay))

    async def stop(self, principal: Principal | None = None) -> None:
        scopes = [principal.org_scope] if principal is not None else list(self._poll_tasks)
        tasks: list[asyncio.Task[None]] = []
        for scope in scopes:
            task = self._poll_tasks.pop(scope, None)
            self._poll_principals.pop(scope, None)
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await self.stop()
        reports = [task for task in self._report_tasks if not task.done()]
        if reports:
            await asyncio.gather(*reports, return_exceptions=True)
        await self.client.close()
