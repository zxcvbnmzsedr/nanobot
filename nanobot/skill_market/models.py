"""Validated control-plane and local-store models for managed Skills."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from nanobot.config_base import Base

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SKILL_KEY_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"

# V1 wire limits. These values are mirrored by the Kangaroo control plane.
MANIFEST_V1_MAX_ENTRIES = 256
MANIFEST_V1_MAX_REVOCATIONS = 4096


class ManifestEntry(Base):
    action: Literal["install", "remove", "disable"]
    release_id: str
    skill_key: str = Field(pattern=_SKILL_KEY_PATTERN)
    version: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_path: str
    signature: str
    signing_key_id: str
    min_runtime_version: str | None = None
    mandatory: bool = False

    @field_validator("release_id", "version", "artifact_path", "signature", "signing_key_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ManifestGeneration(Base):
    global_revision: int = Field(ge=0)
    org_revision: int = Field(ge=0)


class SkillRevocation(Base):
    skill_key: str = Field(pattern=_SKILL_KEY_PATTERN)
    release_id: str
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("release_id")
    @classmethod
    def _release_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class DesiredManifest(Base):
    schema_version: Literal[1]
    revision: str
    generation: ManifestGeneration
    audience: str
    valid_until: datetime
    poll_after_seconds: int = Field(ge=1)
    entries: list[ManifestEntry] = Field(
        default_factory=list,
        max_length=MANIFEST_V1_MAX_ENTRIES,
    )
    revocations: list[SkillRevocation] = Field(
        default_factory=list,
        max_length=MANIFEST_V1_MAX_REVOCATIONS,
    )
    signature: str | None = None
    signing_key_id: str | None = None

    @field_validator("revision", "audience")
    @classmethod
    def _revision_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _unique_skill_entries(self) -> "DesiredManifest":
        seen_skills: set[str] = set()
        for entry in self.entries:
            if entry.skill_key in seen_skills:
                raise ValueError(f"multiple desired entries for Skill: {entry.skill_key}")
            seen_skills.add(entry.skill_key)
        revocation_ids = [
            (item.skill_key, item.release_id, item.sha256)
            for item in self.revocations
        ]
        if len(revocation_ids) != len(set(revocation_ids)):
            raise ValueError("manifest contains duplicate Skill revocations")
        return self

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        expiry = self.valid_until
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry <= current


class ArtifactFile(Base):
    path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size: int = Field(ge=0)
    media_type: str | None = None


class ArtifactManifest(Base):
    schema_version: Literal[1]
    skill_key: str = Field(pattern=_SKILL_KEY_PATTERN)
    version: str
    min_runtime_version: str | None = None
    files: list[ArtifactFile]

    @model_validator(mode="after")
    def _unique_files(self) -> "ArtifactManifest":
        paths = [item.path.casefold() for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact manifest contains duplicate file paths")
        return self


class InstalledSkill(Base):
    skill_key: str = Field(pattern=_SKILL_KEY_PATTERN)
    release_id: str
    version: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    relative_path: str
    mandatory: bool = False
    signing_key_id: str


class SkillSnapshot(Base):
    schema_version: Literal[1] = 1
    revision: str
    snapshot_id: str = Field(pattern=_SHA256_PATTERN)
    activated_at: datetime
    skills: dict[str, InstalledSkill] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _matching_keys(self) -> "SkillSnapshot":
        if any(key != value.skill_key for key, value in self.skills.items()):
            raise ValueError("snapshot Skill map keys do not match entries")
        return self


class SyncResult(Base):
    status: Literal["applied", "unchanged", "failed"]
    revision: str | None = None
    snapshot_id: str | None = None
    poll_after_seconds: int
    changed_skills: list[str] = Field(default_factory=list)
    error_code: str | None = None


class SkillOperationResult(Base):
    status: Literal["applied", "pending", "failed"]
    skill_key: str
    desired_revision: str | None = None
    snapshot_id: str | None = None
    effective: Literal["next_turn"] = "next_turn"
    error_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SkillSyncReport(Base):
    event_id: str = Field(min_length=16, max_length=64)
    correlation_id: str = Field(min_length=8, max_length=128)
    event_type: Literal["sync", "install", "update", "remove", "reject", "rollback"]
    desired_revision: str = Field(default="", max_length=160)
    applied_revision: str = Field(default="", max_length=160)
    skill_key: str | None = Field(default=None, max_length=63)
    release_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$", max_length=32)
    old_digest: str = Field(default="", max_length=64)
    new_digest: str = Field(default="", max_length=64)
    signing_key_id: str = Field(default="", max_length=64)
    status: Literal["success", "failed", "rejected"]
    error_code: str = Field(default="", max_length=64)
    rejection_reason: str = Field(default="", max_length=500)
    runtime_version: str = Field(min_length=1, max_length=32)
