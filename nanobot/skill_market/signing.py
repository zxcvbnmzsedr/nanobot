"""Canonical JSON and Ed25519 verification for Skill control-plane data."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nanobot.skill_market.errors import SkillArtifactError, SkillMarketError


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _decode_public_key(value: str | bytes) -> Ed25519PublicKey:
    raw = value.encode("ascii") if isinstance(value, str) else value
    if raw.startswith(b"-----BEGIN"):
        key = serialization.load_pem_public_key(raw)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("signing key is not Ed25519")
        return key
    try:
        text = raw.decode("ascii").strip()
        decoded = bytes.fromhex(text) if len(text) == 64 else base64.b64decode(text, validate=True)
    except (UnicodeDecodeError, ValueError, binascii.Error) as exc:
        raise ValueError("invalid Ed25519 public key encoding") from exc
    if len(decoded) != 32:
        raise ValueError("Ed25519 public key must contain 32 bytes")
    return Ed25519PublicKey.from_public_bytes(decoded)


def decode_signature(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SkillArtifactError("SIGNATURE_INVALID", "Skill signature is not valid base64") from exc
    if len(decoded) != 64:
        raise SkillArtifactError("SIGNATURE_INVALID", "Skill signature must contain 64 bytes")
    return decoded


def verify_bytes(
    payload: bytes,
    signature: str,
    signing_key_id: str,
    public_keys: Mapping[str, str | bytes],
) -> None:
    encoded_key = public_keys.get(signing_key_id)
    if encoded_key is None:
        raise SkillArtifactError(
            "SIGNING_KEY_UNKNOWN",
            "Skill release uses an unknown signing key",
            details={"signingKeyId": signing_key_id},
        )
    try:
        key = _decode_public_key(encoded_key)
        key.verify(decode_signature(signature), payload)
    except SkillArtifactError:
        raise
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise SkillArtifactError("SIGNATURE_INVALID", "Skill signature verification failed") from exc


def verify_signed_json(
    payload: Mapping[str, Any],
    public_keys: Mapping[str, str | bytes],
    *,
    required: bool = True,
) -> None:
    signature = payload.get("signature")
    key_id = payload.get("signingKeyId", payload.get("signing_key_id"))
    if not signature or not key_id:
        if required:
            raise SkillMarketError(
                "SIGNATURE_REQUIRED",
                "Skill manifest is unsigned",
                http_status=422,
            )
        return
    if not isinstance(signature, str) or not isinstance(key_id, str):
        raise SkillMarketError(
            "SIGNATURE_INVALID",
            "Skill manifest signature metadata is invalid",
            http_status=422,
        )
    signed = dict(payload)
    signed.pop("signature", None)
    try:
        verify_bytes(canonical_json(signed), signature, key_id, public_keys)
    except SkillArtifactError as exc:
        raise SkillMarketError(
            exc.code,
            exc.message,
            http_status=422,
            retryable=False,
            details=exc.details,
        ) from exc
