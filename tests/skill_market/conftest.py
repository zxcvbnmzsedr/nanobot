from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nanobot.skill_market.signing import canonical_json


@pytest.fixture
def signing_material() -> tuple[Ed25519PrivateKey, str, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, "test-key", base64.b64encode(public).decode("ascii")


@pytest.fixture
def artifact_factory(
    signing_material: tuple[Ed25519PrivateKey, str, str],
) -> Callable[..., tuple[bytes, dict[str, Any], dict[str, str]]]:
    private, key_id, _ = signing_material

    def build(
        *,
        skill_key: str = "managed-guide",
        version: str = "1.0.0",
        body: str | None = None,
        references: dict[str, str] | None = None,
        minimum: str | None = None,
        extra_files: dict[str, bytes] | None = None,
    ) -> tuple[bytes, dict[str, Any], dict[str, str]]:
        skill_body = body or (
            f"---\nname: {skill_key}\ndescription: Managed guide\n---\n\n# Managed\n\nSecret body."
        )
        files = {"SKILL.md": skill_body.encode("utf-8")}
        for name, text in (references or {}).items():
            files[f"references/{name}"] = text.encode("utf-8")
        manifest: dict[str, Any] = {
            "schemaVersion": 1,
            "skillKey": skill_key,
            "version": version,
            "minRuntimeVersion": minimum,
            "files": [
                {
                    "path": name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "mediaType": "text/markdown" if name.endswith(".md") else "text/plain",
                }
                for name, content in sorted(files.items())
            ],
        }
        manifest_bytes = canonical_json(manifest)
        signature = base64.b64encode(private.sign(manifest_bytes)).decode("ascii")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("signature.ed25519", signature)
            for name, content in files.items():
                archive.writestr(name, content)
            for name, content in (extra_files or {}).items():
                archive.writestr(name, content)
        artifact = buffer.getvalue()
        digest = hashlib.sha256(artifact).hexdigest()
        entry = {
            "action": "install",
            "releaseId": f"release-{version}",
            "skillKey": skill_key,
            "version": version,
            "sha256": digest,
            "artifactPath": f"/nanobot/skills/releases/release-{version}/artifact",
            "signature": signature,
            "signingKeyId": key_id,
            "minRuntimeVersion": minimum,
            "mandatory": False,
        }
        headers = {
            "Content-Type": "application/zip",
            "X-Skill-Key": skill_key,
            "X-Skill-Version": version,
            "X-Skill-SHA256": digest,
            "X-Skill-Signature": signature,
            "X-Skill-Signing-Key-Id": key_id,
        }
        return artifact, entry, headers

    return build


@pytest.fixture
def manifest_factory(
    signing_material: tuple[Ed25519PrivateKey, str, str],
) -> Callable[..., dict[str, Any]]:
    private, key_id, _ = signing_material

    def build(
        *,
        org_id: str = "org-1",
        global_revision: int = 1,
        org_revision: int = 1,
        digest: str | None = None,
        entries: list[dict[str, Any]] | None = None,
        revocations: list[dict[str, Any]] | None = None,
        valid_until: datetime | None = None,
    ) -> dict[str, Any]:
        audience = f"org:{org_id}"
        generation = {
            "globalRevision": global_revision,
            "orgRevision": org_revision,
        }
        desired_entries = list(entries or [])
        desired_revocations = list(revocations or [])
        revision_digest = digest or hashlib.sha256(
            canonical_json(
                {
                    "audience": audience,
                    "generation": generation,
                    "entries": desired_entries,
                    "revocations": desired_revocations,
                }
            )
        ).hexdigest()
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "revision": f"g{global_revision}-o{org_revision}:{revision_digest}",
            "generation": generation,
            "audience": audience,
            "validUntil": (
                valid_until or datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat(),
            "pollAfterSeconds": 300,
            "entries": desired_entries,
            "revocations": desired_revocations,
            "signingKeyId": key_id,
        }
        payload["signature"] = base64.b64encode(private.sign(canonical_json(payload))).decode("ascii")
        return payload

    return build
