from __future__ import annotations

import hashlib
import io
import random
import stat
import struct
import zipfile
from pathlib import Path
from typing import Any, Callable

import pytest

from nanobot.skill_market.errors import SkillArtifactError
from nanobot.skill_market.models import ManifestEntry
from nanobot.skill_market.package import validate_and_stage_archive
from nanobot.skill_market.settings import SkillPackageLimits


def _stage(
    tmp_path: Path,
    artifact: bytes,
    entry: dict[str, Any],
    headers: dict[str, str],
    public_key: str,
    *,
    public_keys: dict[str, str] | None = None,
    limits: SkillPackageLimits | None = None,
):
    return validate_and_stage_archive(
        artifact,
        ManifestEntry.model_validate(entry),
        public_keys=public_keys if public_keys is not None else {"test-key": public_key},
        staging_root=tmp_path / "staging",
        limits=limits or SkillPackageLimits(),
        runtime_version="1.0.0",
        response_headers=headers,
    )


def _clone_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    cloned = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    cloned.comment = info.comment
    cloned.extra = info.extra
    cloned.create_system = info.create_system
    cloned.create_version = info.create_version
    cloned.extract_version = info.extract_version
    cloned.reserved = info.reserved
    cloned.flag_bits = info.flag_bits
    cloned.volume = info.volume
    cloned.internal_attr = info.internal_attr
    cloned.external_attr = info.external_attr
    cloned.compress_type = info.compress_type
    return cloned


def _zip_entries(artifact: bytes) -> list[tuple[zipfile.ZipInfo, bytes]]:
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(artifact), "r") as archive:
        for info in archive.infolist():
            entries.append((_clone_zip_info(info), b"" if info.is_dir() else archive.read(info)))
    return entries


def _build_zip(entries: list[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, content in entries:
            archive.writestr(info, content)
    return buffer.getvalue()


def _refresh_artifact_metadata(
    artifact: bytes,
    entry: dict[str, Any],
    headers: dict[str, str],
    *,
    signature: str | None = None,
    signing_key_id: str | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    updated_entry = dict(entry)
    updated_headers = dict(headers)
    digest = hashlib.sha256(artifact).hexdigest()
    updated_entry["sha256"] = digest
    updated_headers["X-Skill-SHA256"] = digest
    if signature is not None:
        updated_entry["signature"] = signature
        updated_headers["X-Skill-Signature"] = signature
    if signing_key_id is not None:
        updated_entry["signingKeyId"] = signing_key_id
        updated_headers["X-Skill-Signing-Key-Id"] = signing_key_id
    return artifact, updated_entry, updated_headers


def _zip_entry(name: str, content: bytes, *, mode: int) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = mode << 16
    return info, content


def _mark_zip_encrypted(artifact: bytes) -> bytes:
    data = bytearray(artifact)
    offset = 0
    while offset <= len(data) - 4:
        signature = bytes(data[offset : offset + 4])
        if signature == b"PK\x03\x04":
            flags = struct.unpack_from("<H", data, offset + 6)[0]
            struct.pack_into("<H", data, offset + 6, flags | 0x1)
            name_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
            compressed_size = struct.unpack_from("<I", data, offset + 18)[0]
            offset += 30 + name_length + extra_length + compressed_size
            continue
        if signature == b"PK\x01\x02":
            flags = struct.unpack_from("<H", data, offset + 8)[0]
            struct.pack_into("<H", data, offset + 8, flags | 0x1)
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", data, offset + 28)
            offset += 46 + name_length + extra_length + comment_length
            continue
        if signature == b"PK\x05\x06":
            break
        offset += 1
    return bytes(data)


def _repeat_to_size(seed: str, size: int) -> str:
    return (seed * ((size // len(seed)) + 1))[:size]


def _patterned_text(size: int, *, alphabet: str, chunk_size: int = 2048) -> str:
    generator = random.Random(0)
    chunk = "".join(generator.choice(alphabet) for _ in range(chunk_size))
    return _repeat_to_size(chunk, size)


def test_signed_text_only_archive_is_staged(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory(references={"usage.md": "Use carefully."})
    staged = _stage(tmp_path, artifact, entry, headers, signing_material[2])

    assert (staged.path / "SKILL.md").read_text(encoding="utf-8").endswith("Secret body.")
    assert (staged.path / "references" / "usage.md").read_text() == "Use carefully."
    assert not (staged.path / "manifest.json").exists()


def test_frontmatter_rejects_nested_capability_metadata(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    body = (
        "---\nname: managed-guide\ndescription: Unsafe\n"
        "metadata:\n  nanobot:\n    always: true\n---\n\n# Unsafe"
    )
    artifact, entry, headers = artifact_factory(body=body)

    with pytest.raises(SkillArtifactError, match="only declare name and description") as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "CAPABILITY_ESCALATION"


def test_archive_rejects_unlisted_executable_file(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory(extra_files={"scripts/run.py": b"print('x')"})

    with pytest.raises(SkillArtifactError) as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "CAPABILITY_ESCALATION"


def test_archive_digest_mismatch_is_rejected_before_extraction(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory()
    entry["sha256"] = hashlib.sha256(b"different").hexdigest()

    with pytest.raises(SkillArtifactError) as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "DIGEST_MISMATCH"
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("path_name", ["../escape.md", "/absolute.md", "references\\guide.md"])
def test_archive_rejects_zip_slip_paths(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
    path_name: str,
) -> None:
    artifact, entry, headers = artifact_factory(extra_files={path_name: b"escape"})

    with pytest.raises(SkillArtifactError) as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "ARTIFACT_INVALID"


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o666])
def test_archive_rejects_links_and_special_files(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
    mode: int,
) -> None:
    artifact, entry, headers = artifact_factory()
    entries = _zip_entries(artifact)
    entries.append(_zip_entry("references/device.txt", b"target", mode=mode))
    artifact, entry, headers = _refresh_artifact_metadata(_build_zip(entries), entry, headers)

    with pytest.raises(SkillArtifactError, match="links or special files") as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "ARTIFACT_INVALID"


def test_internal_files_do_not_count_toward_max_files(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory(references={"usage.txt": "Use carefully."})

    staged = _stage(
        tmp_path,
        artifact,
        entry,
        headers,
        signing_material[2],
        limits=SkillPackageLimits(max_files=2),
    )

    assert (staged.path / "SKILL.md").exists()
    assert (staged.path / "references" / "usage.txt").exists()


def test_archive_rejects_too_many_content_files(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory(
        references={"one.txt": "1", "two.txt": "2"}
    )

    with pytest.raises(SkillArtifactError) as error:
        _stage(
            tmp_path,
            artifact,
            entry,
            headers,
            signing_material[2],
            limits=SkillPackageLimits(max_files=2),
        )
    assert error.value.code == "ARTIFACT_TOO_LARGE"


def test_archive_rejects_too_many_entries(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory()
    entries = _zip_entries(artifact)
    entries.extend(
        _zip_entry(f"references/dir-{index}/", b"", mode=stat.S_IFDIR | 0o755)
        for index in range(6)
    )
    artifact, entry, headers = _refresh_artifact_metadata(_build_zip(entries), entry, headers)

    with pytest.raises(SkillArtifactError) as error:
        _stage(
            tmp_path,
            artifact,
            entry,
            headers,
            signing_material[2],
            limits=SkillPackageLimits(max_files=3),
        )
    assert error.value.code == "ARTIFACT_TOO_LARGE"


def test_highly_compressible_backend_text_still_hits_ratio_limit(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    body = (
        "---\nname: managed-guide\ndescription: Managed guide\n---\n\n"
        + ("## Repeated Section\n\n- same line\n" * 4000)
    )
    artifact, entry, headers = artifact_factory(body=body)

    with pytest.raises(SkillArtifactError, match="compression ratio is unsafe") as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "ARTIFACT_TOO_LARGE"


def test_archive_rejects_excess_unpacked_size(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory(
        references={
            "one.txt": _patterned_text(400, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"),
            "two.txt": _patterned_text(400, alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
        }
    )

    with pytest.raises(SkillArtifactError, match="expands past its limit") as error:
        _stage(
            tmp_path,
            artifact,
            entry,
            headers,
            signing_material[2],
            limits=SkillPackageLimits(max_unpacked_bytes=800),
        )
    assert error.value.code == "ARTIFACT_TOO_LARGE"


def test_internal_metadata_does_not_count_toward_unpacked_limit(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    content_size = 512 * 1024
    skill_prefix = "---\nname: managed-guide\ndescription: Managed guide\n---\n\n"
    body = skill_prefix + _patterned_text(
        content_size - len(skill_prefix.encode("utf-8")),
        alphabet="ab",
    )
    references = {
        f"{index}.txt": _patterned_text(content_size, alphabet="ab")
        for index in range(1, 8)
    }
    artifact, entry, headers = artifact_factory(body=body, references=references)

    with zipfile.ZipFile(io.BytesIO(artifact), "r") as archive:
        total_unpacked = sum(info.file_size for info in archive.infolist())
        assert all(
            info.file_size / max(1, info.compress_size) <= SkillPackageLimits().max_compression_ratio
            for info in archive.infolist()
        )
    assert total_unpacked > SkillPackageLimits().max_unpacked_bytes

    staged = _stage(tmp_path, artifact, entry, headers, signing_material[2])

    assert (staged.path / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: managed-guide")
    assert (staged.path / "references" / "7.txt").read_text(encoding="utf-8") == references["7.txt"]


def test_archive_rejects_casefold_duplicate_paths(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory(references={"guide.md": "base"})
    entries = _zip_entries(artifact)
    entries.append(_zip_entry("references/GUIDE.md", b"shadow", mode=stat.S_IFREG | 0o644))
    artifact, entry, headers = _refresh_artifact_metadata(_build_zip(entries), entry, headers)

    with pytest.raises(SkillArtifactError, match="duplicate paths") as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "ARTIFACT_INVALID"


def test_archive_rejects_invalid_signature_encoding(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory()
    entries = [
        (
            info,
            b"not-base64***" if info.filename == "signature.ed25519" else content,
        )
        for info, content in _zip_entries(artifact)
    ]
    artifact, entry, headers = _refresh_artifact_metadata(
        _build_zip(entries),
        entry,
        headers,
        signature="not-base64***",
    )

    with pytest.raises(SkillArtifactError) as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == "SIGNATURE_INVALID"


def test_archive_rejects_unknown_signing_key(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
) -> None:
    artifact, entry, headers = artifact_factory()

    with pytest.raises(SkillArtifactError) as error:
        _stage(
            tmp_path,
            artifact,
            entry,
            headers,
            signing_material[2],
            public_keys={"different-key": signing_material[2]},
        )
    assert error.value.code == "SIGNING_KEY_UNKNOWN"


@pytest.mark.parametrize(
    ("artifact_builder", "expected_code"),
    [
        (lambda artifact: b"not-a-zip", "ARTIFACT_INVALID"),
        (_mark_zip_encrypted, "ARTIFACT_INVALID"),
    ],
)
def test_invalid_or_encrypted_zip_cleans_staging(
    tmp_path: Path,
    artifact_factory: Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]],
    signing_material,
    artifact_builder: Callable[[bytes], bytes],
    expected_code: str,
) -> None:
    artifact, entry, headers = artifact_factory()
    artifact, entry, headers = _refresh_artifact_metadata(
        artifact_builder(artifact),
        entry,
        headers,
    )

    with pytest.raises(SkillArtifactError) as error:
        _stage(tmp_path, artifact, entry, headers, signing_material[2])
    assert error.value.code == expected_code
    assert not (tmp_path / "staging").exists()
