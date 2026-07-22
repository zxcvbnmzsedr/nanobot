from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nanobot.skill_market.errors import SkillMarketError
from nanobot.skill_market.models import InstalledSkill
from nanobot.skill_market.package import StagedRelease
from nanobot.skill_market.store import ManagedSkillStore


def _install(store: ManagedSkillStore, key: str, release: str, digest: str, body: str) -> InstalledSkill:
    relative = store.release_relative_path(key, release, digest)
    skill = InstalledSkill(
        skillKey=key,
        releaseId=release,
        version=release,
        sha256=digest,
        relativePath=relative,
        signingKeyId="key",
    )
    staged = store.staging / f"stage-{release}"
    staged.mkdir(parents=True)
    content = body.encode("utf-8")
    (staged / "SKILL.md").write_bytes(content)
    (staged / ".release.json").write_text(
        json.dumps(
            {
                "releaseId": release,
                "skillKey": key,
                "version": release,
                "sha256": digest,
                "signingKeyId": "key",
                "artifactManifest": {
                    "schemaVersion": 1,
                    "skillKey": key,
                    "version": release,
                    "minRuntimeVersion": None,
                    "files": [
                        {
                            "path": "SKILL.md",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    store.install_staged_release(
        StagedRelease(path=staged, manifest=None, signature=""),  # type: ignore[arg-type]
        skill,
    )
    return skill


def test_current_pointer_recovers_last_known_good(tmp_path: Path) -> None:
    store = ManagedSkillStore(tmp_path / "managed")
    first = _install(store, "guide", "1.0.0", "a" * 64, "first")
    first_snapshot = store.activate("g1-o1:" + "1" * 64, {"guide": first})
    second = _install(store, "guide", "2.0.0", "b" * 64, "second")
    store.activate("g2-o2:" + "2" * 64, {"guide": second})
    store.current_path.write_text("{broken", encoding="utf-8")

    recovered = store.current_snapshot()

    assert recovered is not None
    assert recovered.snapshot_id == first_snapshot.snapshot_id
    assert store.current_path.read_text(encoding="utf-8").startswith("{")


def test_same_revision_and_content_is_idempotent(tmp_path: Path) -> None:
    store = ManagedSkillStore(tmp_path / "managed")
    skill = _install(store, "guide", "1.0.0", "a" * 64, "first")
    revision = "g1-o1:" + "1" * 64

    first = store.activate(revision, {"guide": skill})
    second = store.activate(revision, {"guide": skill})

    assert second.snapshot_id == first.snapshot_id


def test_revoked_digest_is_durable(tmp_path: Path) -> None:
    store = ManagedSkillStore(tmp_path / "managed")
    store.add_revoked_digests({"a" * 64})
    store.add_revoked_digests({"b" * 64})

    assert ManagedSkillStore(store.root).revoked_digests() == {"a" * 64, "b" * 64}


def test_missing_revocation_state_means_no_known_revocations(tmp_path: Path) -> None:
    store = ManagedSkillStore(tmp_path / "managed")

    assert store.revoked_digests() == set()


@pytest.mark.parametrize(
    "raw",
    [
        b"{broken",
        b"\xff",
        b"[]",
        json.dumps({"sha256": []}).encode(),
        json.dumps({"schemaVersion": 2, "sha256": []}).encode(),
        json.dumps({"schemaVersion": True, "sha256": []}).encode(),
        json.dumps({"schemaVersion": 1, "sha256": "a" * 64}).encode(),
        json.dumps({"schemaVersion": 1, "sha256": ["a" * 63]}).encode(),
        json.dumps({"schemaVersion": 1, "sha256": ["A" * 64]}).encode(),
        json.dumps({"schemaVersion": 1, "sha256": ["g" * 64]}).encode(),
    ],
    ids=[
        "json",
        "utf8",
        "non-object",
        "missing-schema",
        "wrong-schema",
        "boolean-schema",
        "non-list-digests",
        "short-digest",
        "uppercase-digest",
        "non-hex-digest",
    ],
)
def test_invalid_revocation_state_fails_closed_without_overwrite(
    tmp_path: Path,
    raw: bytes,
) -> None:
    store = ManagedSkillStore(tmp_path / "managed")
    store.revoked_path.write_bytes(raw)

    with pytest.raises(SkillMarketError) as exc_info:
        store.revoked_digests()

    assert exc_info.value.code == "REVOCATION_STATE_INVALID"
    assert exc_info.value.http_status == 500
    assert store.revoked_path.read_bytes() == raw

    with pytest.raises(SkillMarketError) as add_exc_info:
        store.add_revoked_digests({"b" * 64})

    assert add_exc_info.value.code == "REVOCATION_STATE_INVALID"
    assert store.revoked_path.read_bytes() == raw


def test_post_install_tamper_invalidates_active_snapshot(tmp_path: Path) -> None:
    store = ManagedSkillStore(tmp_path / "managed")
    skill = _install(store, "guide", "1.0.0", "a" * 64, "trusted body")
    snapshot = store.activate("g1-o1:" + "1" * 64, {"guide": skill})
    skill_file = store.release_path(skill) / "SKILL.md"
    skill_file.chmod(0o644)
    skill_file.write_text("tampered body", encoding="utf-8")

    assert store.current_snapshot() is None
    assert not store.has_release(skill)
    with pytest.raises(Exception, match="integrity"):
        store.resolve_skill_file(snapshot, "guide", "SKILL.md")
