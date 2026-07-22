"""Validation and staging for signed, text-only Skill release archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml
from packaging.version import InvalidVersion, Version

from nanobot.skill_market.errors import SkillArtifactError
from nanobot.skill_market.models import ArtifactManifest, ManifestEntry
from nanobot.skill_market.settings import SkillPackageLimits
from nanobot.skill_market.signing import canonical_json, verify_bytes

_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(?P<body>.*?)\r?\n---\s*\r?\n",
    re.DOTALL,
)
_INTERNAL_FILES = frozenset({"manifest.json", "signature.ed25519"})


@dataclass(frozen=True, slots=True)
class StagedRelease:
    path: Path
    manifest: ArtifactManifest
    signature: str


def _artifact_error(code: str, message: str, **details: Any) -> SkillArtifactError:
    return SkillArtifactError(code, message, details=details or None)


def _invalid_zip_error() -> SkillArtifactError:
    return _artifact_error("ARTIFACT_INVALID", "Skill archive ZIP is malformed or unsupported")


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise _artifact_error("ARTIFACT_INVALID", "Skill archive contains an invalid path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise _artifact_error("ARTIFACT_INVALID", "Skill archive path escapes its release root")
    if any(":" in part or any(ord(char) < 32 for char in part) for part in path.parts):
        raise _artifact_error("ARTIFACT_INVALID", "Skill archive contains an unsafe path")
    return path


def _allowed_content_path(path: PurePosixPath) -> bool:
    if path.as_posix() == "SKILL.md":
        return True
    return (
        len(path.parts) >= 2
        and path.parts[0] == "references"
        and path.suffix.lower() in {".md", ".txt"}
    )


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(unix_mode)
    if info.is_dir():
        if kind not in {0, stat.S_IFDIR}:
            raise _artifact_error("ARTIFACT_INVALID", "Skill archive directory type is invalid")
        return
    if kind not in {0, stat.S_IFREG}:
        raise _artifact_error(
            "ARTIFACT_INVALID",
            "Skill archive may not contain links or special files",
            path=info.filename,
        )


def _max_entry_count(limits: SkillPackageLimits) -> int:
    # `max_files` governs user-visible content files (SKILL.md + references/*).
    # Allow one directory entry per content file plus the two signed internal files.
    return len(_INTERNAL_FILES) + (2 * limits.max_files)


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _artifact_error(
                        "ARTIFACT_TOO_LARGE",
                        "Skill archive file exceeds the configured limit",
                        path=info.filename,
                    )
                chunks.append(chunk)
    except SkillArtifactError:
        raise
    except (OSError, NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
        raise _invalid_zip_error() from exc
    if total != info.file_size:
        raise _artifact_error("ARTIFACT_INVALID", "Skill archive file size is inconsistent")
    return b"".join(chunks)


def _decode_text(content: bytes, path: str) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _artifact_error(
            "ARTIFACT_INVALID",
            "Skill archive text must be valid UTF-8",
            path=path,
        ) from exc
    if "\x00" in text:
        raise _artifact_error("ARTIFACT_INVALID", "Skill archive text contains NUL bytes", path=path)
    return text


def _validate_skill_frontmatter(content: bytes, expected_name: str) -> None:
    text = _decode_text(content, "SKILL.md")
    match = _SKILL_FRONTMATTER.match(text)
    if match is None:
        raise _artifact_error("ARTIFACT_INVALID", "SKILL.md requires YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group("body"))
    except yaml.YAMLError as exc:
        raise _artifact_error("ARTIFACT_INVALID", "SKILL.md frontmatter is invalid") from exc
    if not isinstance(frontmatter, dict):
        raise _artifact_error("ARTIFACT_INVALID", "SKILL.md frontmatter must be an object")
    if set(frontmatter) != {"name", "description"}:
        raise _artifact_error(
            "CAPABILITY_ESCALATION",
            "Managed Skills may only declare name and description",
        )
    if frontmatter.get("name") != expected_name:
        raise _artifact_error("ARTIFACT_INVALID", "SKILL.md name does not match the release")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _artifact_error("ARTIFACT_INVALID", "SKILL.md description must not be empty")


def _validate_runtime(minimum: str | None, runtime_version: str) -> None:
    if not minimum:
        return
    try:
        if Version(runtime_version) < Version(minimum):
            raise _artifact_error(
                "RUNTIME_INCOMPATIBLE",
                "Skill release requires a newer nanobot runtime",
                minRuntimeVersion=minimum,
                runtimeVersion=runtime_version,
            )
    except InvalidVersion as exc:
        raise _artifact_error("ARTIFACT_INVALID", "Skill release has an invalid runtime version") from exc


def _write_staged_file(root: Path, relative: PurePosixPath, content: bytes) -> None:
    target = root.joinpath(*relative.parts)
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target == resolved_root or resolved_root not in resolved_target.parents:
        raise _artifact_error("ARTIFACT_INVALID", "Skill archive path escapes staging")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _cleanup_staging_root(staging_root: Path, *, created_root: bool) -> None:
    if not created_root:
        return
    try:
        staging_root.rmdir()
    except OSError:
        pass


def validate_and_stage_archive(
    artifact: bytes,
    entry: ManifestEntry,
    *,
    public_keys: Mapping[str, str | bytes],
    staging_root: Path,
    limits: SkillPackageLimits,
    runtime_version: str,
    response_headers: Mapping[str, str] | None = None,
    reserved_skill_names: set[str] | None = None,
) -> StagedRelease:
    """Fully validate an artifact, then materialize its immutable text files."""
    if len(artifact) > limits.max_archive_bytes:
        raise _artifact_error("ARTIFACT_TOO_LARGE", "Skill archive exceeds the configured limit")
    digest = hashlib.sha256(artifact).hexdigest()
    if digest != entry.sha256:
        raise _artifact_error("DIGEST_MISMATCH", "Skill archive digest does not match the manifest")
    if reserved_skill_names and entry.skill_key in reserved_skill_names:
        raise _artifact_error(
            "SKILL_COLLISION",
            "Managed Skill conflicts with a built-in Skill",
            skillKey=entry.skill_key,
        )
    _validate_runtime(entry.min_runtime_version, runtime_version)

    headers = {str(key).lower(): str(value) for key, value in (response_headers or {}).items()}
    expected_headers = {
        "x-skill-key": entry.skill_key,
        "x-skill-version": entry.version,
        "x-skill-sha256": entry.sha256,
        "x-skill-signature": entry.signature,
        "x-skill-signing-key-id": entry.signing_key_id,
    }
    for key, expected in expected_headers.items():
        actual = headers.get(key)
        if actual is not None and actual != expected:
            raise _artifact_error("ARTIFACT_INVALID", "Skill artifact headers are inconsistent")

    created_staging_root = not staging_root.exists()
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=f"{entry.skill_key}-", dir=staging_root))
        import io

        try:
            archive = zipfile.ZipFile(io.BytesIO(artifact), "r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise _invalid_zip_error() from exc

        with archive:
            infos = archive.infolist()
            if len(infos) > _max_entry_count(limits):
                raise _artifact_error("ARTIFACT_TOO_LARGE", "Skill archive contains too many entries")
            content_infos = [
                info
                for info in infos
                if not info.is_dir() and info.filename not in _INTERNAL_FILES
            ]
            if len(content_infos) > limits.max_files:
                raise _artifact_error("ARTIFACT_TOO_LARGE", "Skill archive contains too many files")
            seen: set[str] = set()
            unpacked = 0
            contents: dict[str, bytes] = {}
            for info in infos:
                _validate_member_type(info)
                if info.flag_bits & 0x1:
                    raise _artifact_error(
                        "ARTIFACT_INVALID",
                        "Skill archive may not contain encrypted files",
                        path=info.filename,
                    )
                path = _safe_member_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                folded = path.as_posix().casefold()
                if folded in seen:
                    raise _artifact_error("ARTIFACT_INVALID", "Skill archive has duplicate paths")
                seen.add(folded)
                if info.is_dir():
                    if path.parts[0] != "references":
                        raise _artifact_error("ARTIFACT_INVALID", "Skill archive has an extra directory")
                    continue
                is_internal = path.as_posix() in _INTERNAL_FILES
                if not is_internal and not _allowed_content_path(path):
                    raise _artifact_error(
                        "CAPABILITY_ESCALATION",
                        "Managed Skill archive contains a disallowed file",
                        path=path.as_posix(),
                    )
                if info.file_size > limits.max_file_bytes:
                    raise _artifact_error("ARTIFACT_TOO_LARGE", "Skill archive file is too large")
                if not is_internal:
                    unpacked += info.file_size
                    if unpacked > limits.max_unpacked_bytes:
                        raise _artifact_error(
                            "ARTIFACT_TOO_LARGE",
                            "Skill archive expands past its limit",
                        )
                ratio = info.file_size / max(1, info.compress_size)
                if ratio > limits.max_compression_ratio:
                    raise _artifact_error("ARTIFACT_TOO_LARGE", "Skill archive compression ratio is unsafe")
                contents[path.as_posix()] = _read_member(
                    archive,
                    info,
                    max_bytes=limits.max_file_bytes,
                )

        if set(_INTERNAL_FILES) - contents.keys() or "SKILL.md" not in contents:
            raise _artifact_error("ARTIFACT_INVALID", "Skill archive is missing required files")
        manifest_text = _decode_text(contents["manifest.json"], "manifest.json")
        try:
            raw_manifest = json.loads(manifest_text)
            if not isinstance(raw_manifest, dict):
                raise ValueError("manifest is not an object")
            inner = ArtifactManifest.model_validate(raw_manifest)
        except (ValueError, TypeError) as exc:
            raise _artifact_error("ARTIFACT_INVALID", "Skill artifact manifest is invalid") from exc
        canonical_manifest = canonical_json(raw_manifest)
        if contents["manifest.json"] != canonical_manifest:
            raise _artifact_error("ARTIFACT_INVALID", "Skill artifact manifest is not canonical JSON")
        signature = _decode_text(contents["signature.ed25519"], "signature.ed25519").strip()
        if signature != entry.signature:
            raise _artifact_error("SIGNATURE_INVALID", "Skill signatures do not match")
        verify_bytes(canonical_manifest, signature, entry.signing_key_id, public_keys)

        if inner.skill_key != entry.skill_key or inner.version != entry.version:
            raise _artifact_error("ARTIFACT_INVALID", "Skill artifact identity does not match release")
        if inner.min_runtime_version != entry.min_runtime_version:
            raise _artifact_error("ARTIFACT_INVALID", "Skill runtime requirements are inconsistent")
        _validate_runtime(inner.min_runtime_version, runtime_version)

        declared = {item.path: item for item in inner.files}
        content_paths = set(contents) - _INTERNAL_FILES
        if set(declared) != content_paths or "SKILL.md" not in declared:
            raise _artifact_error("ARTIFACT_INVALID", "Skill artifact file manifest is incomplete")
        for relative, file_info in declared.items():
            path = _safe_member_path(relative)
            if not _allowed_content_path(path):
                raise _artifact_error("CAPABILITY_ESCALATION", "Skill manifest declares a disallowed file")
            content = contents[relative]
            if len(content) != file_info.size or hashlib.sha256(content).hexdigest() != file_info.sha256:
                raise _artifact_error("DIGEST_MISMATCH", "Skill file digest does not match manifest")
            expected_media = "text/markdown" if path.suffix.lower() == ".md" else "text/plain"
            if file_info.media_type is not None and file_info.media_type != expected_media:
                raise _artifact_error("ARTIFACT_INVALID", "Skill file media type is invalid")
            _decode_text(content, relative)

        _validate_skill_frontmatter(contents["SKILL.md"], entry.skill_key)
        for relative in sorted(content_paths):
            _write_staged_file(temporary, _safe_member_path(relative), contents[relative])
        release_metadata = {
            "schemaVersion": 1,
            "releaseId": entry.release_id,
            "skillKey": entry.skill_key,
            "version": entry.version,
            "sha256": entry.sha256,
            "signingKeyId": entry.signing_key_id,
            "signature": signature,
            "artifactManifest": raw_manifest,
        }
        _write_staged_file(temporary, PurePosixPath(".release.json"), canonical_json(release_metadata))
        return StagedRelease(path=temporary, manifest=inner, signature=signature)
    except Exception:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        _cleanup_staging_root(staging_root, created_root=created_staging_root)
        raise
