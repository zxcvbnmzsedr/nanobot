from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from nanobot.skill_market.models import (
    MANIFEST_V1_MAX_ENTRIES,
    MANIFEST_V1_MAX_REVOCATIONS,
    DesiredManifest,
)


def _entry(index: int) -> dict[str, Any]:
    return {
        "action": "install",
        "releaseId": str(index + 1),
        "skillKey": f"skill-{index}",
        "version": "1.0.0",
        "sha256": f"{index:064x}",
        "artifactPath": f"/nanobot/skills/releases/{index + 1}/artifact",
        "signature": "artifact-signature",
        "signingKeyId": "release-key",
        "minRuntimeVersion": "0.1.0",
        "mandatory": False,
    }


def _revocation(index: int) -> dict[str, Any]:
    return {
        "skillKey": "revoked-skill",
        "releaseId": str(index + 1),
        "sha256": f"{index:064x}",
    }


def _manifest_payload(*, entry_count: int, revocation_count: int) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "revision": "g1-o1:" + "a" * 64,
        "generation": {"globalRevision": 1, "orgRevision": 1},
        "audience": "org:test-org",
        "validUntil": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "pollAfterSeconds": 300,
        "entries": [_entry(index) for index in range(entry_count)],
        "revocations": [_revocation(index) for index in range(revocation_count)],
        "signingKeyId": "manifest-key",
        "signature": "manifest-signature",
    }


@pytest.mark.parametrize(
    ("entry_count", "revocation_count"),
    [
        (MANIFEST_V1_MAX_ENTRIES, 0),
        (0, MANIFEST_V1_MAX_REVOCATIONS),
    ],
)
def test_manifest_v1_accepts_exact_capacity_limits(
    entry_count: int,
    revocation_count: int,
) -> None:
    manifest = DesiredManifest.model_validate(
        _manifest_payload(entry_count=entry_count, revocation_count=revocation_count)
    )

    assert len(manifest.entries) == entry_count
    assert len(manifest.revocations) == revocation_count


@pytest.mark.parametrize(
    ("entry_count", "revocation_count"),
    [
        (MANIFEST_V1_MAX_ENTRIES + 1, 0),
        (0, MANIFEST_V1_MAX_REVOCATIONS + 1),
    ],
)
def test_manifest_v1_rejects_capacity_limit_plus_one(
    entry_count: int,
    revocation_count: int,
) -> None:
    with pytest.raises(ValidationError):
        DesiredManifest.model_validate(
            _manifest_payload(entry_count=entry_count, revocation_count=revocation_count)
        )
