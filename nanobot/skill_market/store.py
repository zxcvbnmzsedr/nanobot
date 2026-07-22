"""Crash-safe, immutable local storage for institution-managed Skills."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock

from nanobot.identity.principal import IDENTITY_METADATA_KEY
from nanobot.skill_market.errors import SkillArtifactError, SkillMarketError
from nanobot.skill_market.models import ArtifactManifest, InstalledSkill, SkillSnapshot
from nanobot.skill_market.package import StagedRelease
from nanobot.skill_market.signing import canonical_json

SKILL_SNAPSHOT_METADATA_KEY = "_skill_market_snapshot"
_CURRENT = "CURRENT.json"
_PREVIOUS = "PREVIOUS.json"
_SYNC_STATE = "sync-state.json"
_REVOKED = "revoked.json"
_SHA256_HEX = frozenset("0123456789abcdef")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def managed_skills_path_from_metadata(metadata: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(metadata, Mapping):
        return None
    identity = metadata.get(IDENTITY_METADATA_KEY)
    if not isinstance(identity, Mapping):
        return None
    raw = identity.get("managed_skills_path")
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def snapshot_from_metadata(metadata: Mapping[str, Any] | None) -> SkillSnapshot | None:
    if not isinstance(metadata, Mapping):
        return None
    payload = metadata.get(SKILL_SNAPSHOT_METADATA_KEY)
    if not isinstance(payload, Mapping):
        return None
    try:
        return SkillSnapshot.model_validate(payload)
    except ValueError:
        return None


def pin_snapshot_in_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy request metadata and attach exactly one validated current snapshot."""
    pinned = dict(metadata or {})
    if snapshot_from_metadata(pinned) is not None:
        return pinned
    root = managed_skills_path_from_metadata(pinned)
    if root is None:
        return pinned
    snapshot = ManagedSkillStore(root).current_snapshot()
    if snapshot is not None:
        pinned[SKILL_SNAPSHOT_METADATA_KEY] = snapshot.model_dump(mode="json", by_alias=True)
    return pinned


class ManagedSkillStore:
    """One organization-scoped release store with atomic snapshot activation."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.releases = self.root / "releases"
        self.revisions = self.root / "revisions"
        self.staging = self.root / "staging"
        self.quarantine = self.root / "quarantine"
        self.locks = self.root / "locks"
        for directory in (
            self.releases,
            self.revisions,
            self.staging,
            self.quarantine,
            self.locks,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self.locks / "sync.lock"), timeout=30)

    @property
    def current_path(self) -> Path:
        return self.root / _CURRENT

    @property
    def previous_path(self) -> Path:
        return self.root / _PREVIOUS

    @property
    def sync_state_path(self) -> Path:
        return self.root / _SYNC_STATE

    @property
    def revoked_path(self) -> Path:
        return self.root / _REVOKED

    def _revision_path(self, revision: str) -> Path:
        filename = hashlib.sha256(revision.encode("utf-8")).hexdigest() + ".json"
        return self.revisions / filename

    def _pointer_payload(self, snapshot: SkillSnapshot) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "revision": snapshot.revision,
            "snapshotId": snapshot.snapshot_id,
            "revisionFile": self._revision_path(snapshot.revision).name,
        }

    def _load_pointer(self, path: Path) -> SkillSnapshot | None:
        pointer = _read_json(path)
        if pointer is None or pointer.get("schemaVersion") != 1:
            return None
        revision_file = pointer.get("revisionFile")
        if not isinstance(revision_file, str) or Path(revision_file).name != revision_file:
            return None
        candidate = (self.revisions / revision_file).resolve(strict=False)
        if candidate.parent != self.revisions.resolve(strict=False):
            return None
        payload = _read_json(candidate)
        if payload is None:
            return None
        try:
            snapshot = SkillSnapshot.model_validate(payload)
        except ValueError:
            return None
        if (
            pointer.get("revision") != snapshot.revision
            or pointer.get("snapshotId") != snapshot.snapshot_id
            or not self._validate_snapshot_paths(snapshot)
        ):
            return None
        return snapshot

    def _validate_snapshot_paths(self, snapshot: SkillSnapshot) -> bool:
        releases_root = self.releases.resolve(strict=False)
        for skill in snapshot.skills.values():
            relative = PurePosixPath(skill.relative_path)
            if relative.is_absolute() or ".." in relative.parts or "\\" in skill.relative_path:
                return False
            release_root = self.root.joinpath(*relative.parts).resolve(strict=False)
            if release_root == releases_root or releases_root not in release_root.parents:
                return False
            if not self._validate_release_path(skill, release_root):
                return False
        return True

    @staticmethod
    def _validate_release_path(skill: InstalledSkill, path: Path) -> bool:
        metadata = _read_json(path / ".release.json")
        if (
            metadata is None
            or metadata.get("sha256") != skill.sha256
            or metadata.get("releaseId") != skill.release_id
            or metadata.get("skillKey") != skill.skill_key
            or metadata.get("version") != skill.version
            or metadata.get("signingKeyId") != skill.signing_key_id
        ):
            return False
        raw_manifest = metadata.get("artifactManifest")
        try:
            manifest = ArtifactManifest.model_validate(raw_manifest)
        except ValueError:
            return False
        if manifest.skill_key != skill.skill_key or manifest.version != skill.version:
            return False

        actual_paths: set[str] = set()
        try:
            for directory, dir_names, file_names in os.walk(path, followlinks=False):
                directory_path = Path(directory)
                for name in dir_names:
                    if (directory_path / name).is_symlink():
                        return False
                for name in file_names:
                    candidate = directory_path / name
                    if candidate.is_symlink() or not candidate.is_file():
                        return False
                    relative = candidate.relative_to(path).as_posix()
                    if relative != ".release.json":
                        actual_paths.add(relative)
        except (OSError, ValueError):
            return False

        declared = {item.path: item for item in manifest.files}
        if actual_paths != set(declared):
            return False
        for relative, expected in declared.items():
            target = path.joinpath(*PurePosixPath(relative).parts)
            try:
                if target.stat().st_size != expected.size:
                    return False
                digest = hashlib.sha256()
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(64 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected.sha256:
                    return False
            except OSError:
                return False
        return True

    def current_snapshot(self) -> SkillSnapshot | None:
        current = self._load_pointer(self.current_path)
        if current is not None:
            return current
        previous = self._load_pointer(self.previous_path)
        if previous is None:
            return None
        with self._lock:
            if self._load_pointer(self.current_path) is None:
                _atomic_write(self.current_path, canonical_json(self._pointer_payload(previous)))
        return previous

    def sync_state(self) -> dict[str, Any]:
        return _read_json(self.sync_state_path) or {}

    def update_sync_state(self, **updates: Any) -> dict[str, Any]:
        with self._lock:
            state = self.sync_state()
            state.update(updates)
            _atomic_write(self.sync_state_path, canonical_json(state))
            return state

    def revoked_digests(self) -> set[str]:
        try:
            raw = self.revoked_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillMarketError(
                "REVOCATION_STATE_INVALID",
                "Managed Skill revocation state is invalid",
                http_status=500,
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SkillMarketError(
                "REVOCATION_STATE_INVALID",
                "Managed Skill revocation state is invalid",
                http_status=500,
            ) from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("schemaVersion")) is not int
            or payload["schemaVersion"] != 1
            or not isinstance(payload.get("sha256"), list)
        ):
            raise SkillMarketError(
                "REVOCATION_STATE_INVALID",
                "Managed Skill revocation state is invalid",
                http_status=500,
            )
        values = payload["sha256"]
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in _SHA256_HEX for character in value)
            for value in values
        ):
            raise SkillMarketError(
                "REVOCATION_STATE_INVALID",
                "Managed Skill revocation state is invalid",
                http_status=500,
            )
        return set(values)

    def add_revoked_digests(self, digests: set[str]) -> None:
        if not digests:
            return
        with self._lock:
            combined = self.revoked_digests() | digests
            _atomic_write(
                self.revoked_path,
                canonical_json({"schemaVersion": 1, "sha256": sorted(combined)}),
            )

    def release_relative_path(self, skill_key: str, release_id: str, digest: str) -> str:
        safe_release = hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:16]
        return f"releases/{skill_key}/{safe_release}-{digest[:16]}"

    def release_path(self, skill: InstalledSkill) -> Path:
        relative = PurePosixPath(skill.relative_path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in skill.relative_path:
            raise SkillMarketError("STORE_INVALID", "Managed Skill path is invalid", http_status=500)
        path = self.root.joinpath(*relative.parts).resolve(strict=False)
        releases_root = self.releases.resolve(strict=False)
        if path == releases_root or releases_root not in path.parents:
            raise SkillMarketError("STORE_INVALID", "Managed Skill path escapes its store", http_status=500)
        return path

    def has_release(self, skill: InstalledSkill) -> bool:
        path = self.release_path(skill)
        return self._validate_release_path(skill, path)

    @staticmethod
    def _make_tree_read_only(path: Path) -> None:
        for directory, dir_names, file_names in os.walk(path, topdown=False, followlinks=False):
            root = Path(directory)
            for name in file_names:
                (root / name).chmod(0o444)
            for name in dir_names:
                (root / name).chmod(0o555)
        path.chmod(0o555)

    def install_staged_release(self, staged: StagedRelease, skill: InstalledSkill) -> Path:
        target = self.release_path(skill)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if target.exists():
                if self.has_release(skill):
                    shutil.rmtree(staged.path, ignore_errors=True)
                    return target
                raise SkillArtifactError(
                    "IMMUTABLE_RELEASE_CONFLICT",
                    "A release path already exists with different content",
                )
            os.replace(staged.path, target)
            self._make_tree_read_only(target)
            _fsync_directory(target.parent)
        return target

    def activate(self, revision: str, skills: dict[str, InstalledSkill]) -> SkillSnapshot:
        for skill in skills.values():
            if not self.has_release(skill):
                raise SkillMarketError(
                    "STORE_INVALID",
                    "Cannot activate an incomplete Skill release",
                    http_status=500,
                )
        current = self.current_snapshot()
        if current is not None and current.revision == revision:
            if current.skills != skills:
                raise SkillMarketError(
                    "REVISION_CONFLICT",
                    "Manifest revision already refers to a different snapshot",
                    http_status=409,
                )
            return current
        revision_path = self._revision_path(revision)
        if revision_path.exists():
            payload = _read_json(revision_path)
            try:
                existing = SkillSnapshot.model_validate(payload)
            except ValueError as exc:
                raise SkillMarketError(
                    "STORE_INVALID",
                    "Stored Skill revision is invalid",
                    http_status=500,
                ) from exc
            if (
                existing.revision != revision
                or existing.skills != skills
                or not self._validate_snapshot_paths(existing)
            ):
                raise SkillMarketError(
                    "REVISION_CONFLICT",
                    "Manifest revision already refers to a different snapshot",
                    http_status=409,
                )
            with self._lock:
                _atomic_write(
                    self.current_path,
                    canonical_json(self._pointer_payload(existing)),
                )
            return existing
        snapshot_body = {
            "schemaVersion": 1,
            "revision": revision,
            "activatedAt": datetime.now(timezone.utc).isoformat(),
            "skills": {
                key: value.model_dump(mode="json", by_alias=True)
                for key, value in sorted(skills.items())
            },
        }
        snapshot_id = hashlib.sha256(canonical_json(snapshot_body)).hexdigest()
        snapshot = SkillSnapshot.model_validate({**snapshot_body, "snapshotId": snapshot_id})
        encoded = canonical_json(snapshot.model_dump(mode="json", by_alias=True))
        with self._lock:
            if revision_path.exists():
                if revision_path.read_bytes() != encoded:
                    raise SkillMarketError(
                        "REVISION_CONFLICT",
                        "Manifest revision already refers to a different snapshot",
                        http_status=409,
                    )
            else:
                _atomic_write(revision_path, encoded)
            current = self._load_pointer(self.current_path)
            if current is not None and current.snapshot_id != snapshot.snapshot_id:
                _atomic_write(
                    self.previous_path,
                    canonical_json(self._pointer_payload(current)),
                )
            _atomic_write(
                self.current_path,
                canonical_json(self._pointer_payload(snapshot)),
            )
        return snapshot

    def quarantine_artifact(
        self,
        artifact: bytes,
        *,
        digest: str,
        code: str,
        message: str,
    ) -> Path:
        suffix = hashlib.sha256(f"{time.time_ns()}:{digest}".encode()).hexdigest()[:12]
        target = self.quarantine / f"{int(time.time())}-{digest[:12]}-{suffix}"
        target.mkdir(parents=True, exist_ok=False)
        _atomic_write(target / "artifact.zip", artifact)
        _atomic_write(
            target / "rejection.json",
            canonical_json(
                {
                    "schemaVersion": 1,
                    "sha256": digest,
                    "code": code,
                    "message": message,
                    "quarantinedAt": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
        return target

    def resolve_skill_file(
        self,
        snapshot: SkillSnapshot,
        skill_key: str,
        relative_path: str,
    ) -> tuple[InstalledSkill, Path]:
        skill = snapshot.skills.get(skill_key)
        if skill is None:
            raise SkillMarketError("SKILL_NOT_INSTALLED", "Managed Skill is not installed", http_status=404)
        if not self.has_release(skill):
            raise SkillMarketError(
                "RELEASE_INTEGRITY_FAILED",
                "Managed Skill release failed its local integrity check",
                http_status=409,
            )
        requested = PurePosixPath(relative_path)
        if (
            requested.is_absolute()
            or ".." in requested.parts
            or "\\" in relative_path
            or not (
                requested.as_posix() == "SKILL.md"
                or (
                    len(requested.parts) >= 2
                    and requested.parts[0] == "references"
                    and requested.suffix.lower() in {".md", ".txt"}
                )
            )
        ):
            raise SkillMarketError("INVALID_REFERENCE", "Skill reference path is invalid")
        release_root = self.release_path(skill)
        target = release_root.joinpath(*requested.parts).resolve(strict=False)
        if target == release_root or release_root not in target.parents or not target.is_file():
            raise SkillMarketError("REFERENCE_NOT_FOUND", "Skill reference was not found", http_status=404)
        return skill, target
