from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock

import httpx
import pytest

from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.read_skill import ReadSkillTool
from nanobot.identity.principal import IDENTITY_METADATA_KEY, Principal
from nanobot.skill_market.errors import SkillMarketError
from nanobot.skill_market.models import SkillOperationResult, SkillSyncReport, SyncResult
from nanobot.skill_market.service import SkillMarketService
from nanobot.skill_market.settings import SkillMarketSettings
from nanobot.skill_market.store import SKILL_SNAPSHOT_METADATA_KEY


class _Credentials:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def get_valid_access_token(self, user_scope: str) -> str | None:
        self.scopes.append(user_scope)
        return "access-token"

    async def refresh_access_token(
        self,
        user_scope: str,
        *,
        rejected_access_token: str,
    ) -> str | None:
        return None


def _service(
    tmp_path: Path,
    public_key: str,
    handler: Callable[[httpx.Request], httpx.Response],
) -> SkillMarketService:
    return SkillMarketService(
        SkillMarketSettings(
            base_url="https://control.example",
            runtime_root=tmp_path / "runtime",
            public_keys={"test-key": public_key},
            enabled=True,
        ),
        _Credentials(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        builtin_skills_dir=tmp_path / "builtin",
    )


def test_skill_market_settings_require_an_origin_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"HTTP\(S\) origin"):
        SkillMarketSettings(
            enabled=True,
            base_url="https://control.example/prefix",
            runtime_root=tmp_path,
            public_keys={"key-1": "public-key"},
        )


@pytest.mark.asyncio
async def test_sync_uses_bearer_etag_and_publishes_activation(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifest = manifest_factory(entries=[entry])
    etag = f'"{manifest["revision"]}"'
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer access-token"
        if request.url.path == "/nanobot/skills/manifest":
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"ETag": etag})
            return httpx.Response(200, json=manifest, headers={"ETag": etag})
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    events: list[dict[str, Any]] = []
    service.subscribe(events.append)

    first = await service.sync_now(principal)
    second = await service.sync_now(principal)

    assert first.status == "applied"
    assert first.changed_skills == ["managed-guide"]
    assert second.status == "unchanged"
    assert events[0]["orgScope"] == principal.org_scope
    assert events[0]["previousSnapshotId"] is None
    state = service._store(principal).sync_state()
    assert state["lastSuccessAt"]
    manifest_requests = [request for request in requests if request.url.path.endswith("/manifest")]
    assert manifest_requests[-1].headers["if-none-match"] == etag
    await service.close()


@pytest.mark.asyncio
async def test_policy_change_publishes_when_active_skill_content_is_unchanged(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifests = [
        manifest_factory(entries=[entry]),
        manifest_factory(global_revision=2, org_revision=2, entries=[entry]),
    ]
    policy_written = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal policy_written
        if request.url.path == "/nanobot/skills/manifest":
            manifest = manifests[int(policy_written)]
            return httpx.Response(
                200,
                json=manifest,
                headers={"ETag": f'"{manifest["revision"]}"'},
            )
        if request.method == "PUT":
            policy_written = True
            return httpx.Response(200, json={"rowVersion": 2})
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    events: list[dict[str, Any]] = []
    service.subscribe(events.append)
    assert (await service.sync_now(principal, force=True)).status == "applied"
    previous = service._store(principal).current_snapshot()
    assert previous is not None
    events.clear()

    result = await service.set_policy(
        principal,
        "managed-guide",
        update_policy="notify",
        expected_row_version=1,
    )

    current = service._store(principal).current_snapshot()
    assert current is not None
    assert current.skills == previous.skills
    assert result.status == "applied"
    assert events == [{
        "type": "skills_updated",
        "orgScope": principal.org_scope,
        "revision": current.revision,
        "snapshotId": current.snapshot_id,
        "reason": "policy",
        "changed": [],
        "skillKey": "managed-guide",
    }]
    await service.close()


@pytest.mark.asyncio
async def test_operation_with_activation_change_publishes_once(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifest = manifest_factory(entries=[entry])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            return httpx.Response(
                200,
                json=manifest,
                headers={"ETag": f'"{manifest["revision"]}"'},
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"rowVersion": 1})
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    events: list[dict[str, Any]] = []
    service.subscribe(events.append)

    result = await service.install(
        principal,
        "managed-guide",
        update_policy="notify",
    )

    assert result.status == "applied"
    assert len(events) == 1
    assert events[0]["reason"] == "install"
    assert events[0]["changed"] == ["managed-guide"]
    await service.close()


@pytest.mark.asyncio
async def test_failed_operation_sync_does_not_publish(
    tmp_path: Path,
    signing_material,
) -> None:
    service = _service(
        tmp_path,
        signing_material[2],
        lambda request: httpx.Response(500),
    )
    principal = Principal(user_id="user-1", org_id="org-1")
    events: list[dict[str, Any]] = []
    service.subscribe(events.append)
    service._sync_now = AsyncMock(return_value=SyncResult(
        status="failed",
        pollAfterSeconds=300,
        errorCode="CONTROL_PLANE_UNAVAILABLE",
    ))

    result = await service._operation_sync(
        principal,
        "managed-guide",
        {"rowVersion": 2},
        reason="policy",
    )

    assert result.status == "failed"
    assert events == []
    await service.close()


@pytest.mark.parametrize("weak", [False, True], ids=["strong", "weak"])
@pytest.mark.asyncio
async def test_manifest_etag_accepts_strong_and_weak_forms(
    tmp_path: Path,
    manifest_factory,
    signing_material,
    weak: bool,
) -> None:
    manifest = manifest_factory()
    prefix = "W/" if weak else ""
    etag = f'{prefix}"{manifest["revision"]}"'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=manifest, headers={"ETag": etag})

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")

    result = await service.sync_now(principal, force=True)

    assert result.status == "unchanged"
    assert result.error_code is None
    assert service._store(principal).sync_state()["etag"] == etag
    await service.close()


@pytest.mark.asyncio
async def test_manifest_etag_requires_exact_revision_match(
    tmp_path: Path,
    manifest_factory,
    signing_material,
) -> None:
    manifest = manifest_factory()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=manifest, headers={"ETag": 'W/"other-revision"'})

    service = _service(tmp_path, signing_material[2], handler)

    result = await service.sync_now(
        Principal(user_id="user-1", org_id="org-1"),
        force=True,
    )

    assert result.status == "failed"
    assert result.error_code == "MANIFEST_ETAG_MISMATCH"
    await service.close()


@pytest.mark.asyncio
async def test_manifest_audience_is_bound_to_verified_principal(
    tmp_path: Path,
    manifest_factory,
    signing_material,
) -> None:
    manifest = manifest_factory(org_id="other-org")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})

    service = _service(tmp_path, signing_material[2], handler)
    result = await service.sync_now(Principal(user_id="user-1", org_id="org-1"))

    assert result.status == "failed"
    assert result.error_code == "MANIFEST_AUDIENCE_MISMATCH"
    await service.close()


@pytest.mark.asyncio
async def test_manifest_replay_and_same_generation_fork_are_rejected(
    tmp_path: Path,
    manifest_factory,
    signing_material,
) -> None:
    current = manifest_factory(global_revision=5, org_revision=4)
    response_manifest = current

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_manifest,
            headers={"ETag": f'"{response_manifest["revision"]}"'},
        )

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "unchanged"

    response_manifest = manifest_factory(global_revision=4, org_revision=4)
    replay = await service.sync_now(principal, force=True)
    assert replay.error_code == "MANIFEST_REPLAYED"

    response_manifest = manifest_factory(
        global_revision=5,
        org_revision=4,
        revocations=[
            {"skillKey": "managed-guide", "releaseId": "1", "sha256": "c" * 64}
        ],
    )
    fork = await service.sync_now(principal, force=True)
    assert fork.error_code == "REVISION_CONFLICT"
    assert service._store(principal).current_snapshot().revision == current["revision"]
    await service.close()


@pytest.mark.asyncio
async def test_new_release_can_activate_while_previous_release_is_revoked(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    old_artifact, old_entry, old_headers = artifact_factory(version="1.0.0")
    new_artifact, new_entry, new_headers = artifact_factory(version="2.0.0")
    manifests = [
        manifest_factory(entries=[old_entry]),
        manifest_factory(
            global_revision=2,
            org_revision=2,
            entries=[new_entry],
            revocations=[
                {
                    "skillKey": old_entry["skillKey"],
                    "releaseId": old_entry["releaseId"],
                    "sha256": old_entry["sha256"],
                }
            ],
        ),
    ]
    index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            manifest = manifests[index]
            return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})
        if request.url.path.endswith("release-1.0.0/artifact"):
            return httpx.Response(200, content=old_artifact, headers=old_headers)
        return httpx.Response(200, content=new_artifact, headers=new_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"
    store = service._store(principal)
    pinned_v1 = store.current_snapshot()
    assert pinned_v1 is not None
    index = 1
    assert (await service.sync_now(principal, force=True)).status == "applied"

    snapshot = store.current_snapshot()
    assert snapshot.skills["managed-guide"].version == "2.0.0"
    assert old_entry["sha256"] in store.revoked_digests()
    metadata = {
        IDENTITY_METADATA_KEY: {"managed_skills_path": str(store.root)},
        SKILL_SNAPSHOT_METADATA_KEY: pinned_v1.model_dump(mode="json", by_alias=True),
    }
    with request_context(RequestContext(channel="test", chat_id="1", metadata=metadata)):
        result = await ReadSkillTool().execute("managed-guide")
    assert "has been revoked" in result
    await service.close()


@pytest.mark.asyncio
async def test_policy_disable_does_not_permanently_revoke_release(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifests = [
        manifest_factory(entries=[entry]),
        manifest_factory(
            global_revision=2,
            org_revision=2,
            entries=[{**entry, "action": "disable"}],
        ),
        manifest_factory(global_revision=3, org_revision=3, entries=[entry]),
    ]
    index = 0
    artifact_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal artifact_requests
        if request.url.path == "/nanobot/skills/manifest":
            manifest = manifests[index]
            return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        artifact_requests += 1
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"

    index = 1
    assert (await service.sync_now(principal, force=True)).status == "applied"
    store = service._store(principal)
    assert store.current_snapshot().skills == {}
    assert entry["sha256"] not in store.revoked_digests()

    index = 2
    assert (await service.sync_now(principal, force=True)).status == "applied"
    assert store.current_snapshot().skills["managed-guide"].version == "1.0.0"
    assert artifact_requests == 1
    await service.close()


@pytest.mark.asyncio
async def test_revocation_is_persisted_before_replacement_artifact_failure(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    old_artifact, old_entry, old_headers = artifact_factory(version="1.0.0")
    _, new_entry, new_headers = artifact_factory(version="2.0.0")
    manifests = [
        manifest_factory(entries=[old_entry]),
        manifest_factory(
            global_revision=2,
            org_revision=2,
            entries=[new_entry],
            revocations=[
                {
                    "skillKey": old_entry["skillKey"],
                    "releaseId": old_entry["releaseId"],
                    "sha256": old_entry["sha256"],
                }
            ],
        ),
    ]
    index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            manifest = manifests[index]
            return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})
        if request.url.path.endswith("release-1.0.0/artifact"):
            return httpx.Response(200, content=old_artifact, headers=old_headers)
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=b"not-a-zip", headers=new_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"
    store = service._store(principal)
    pinned = store.current_snapshot()
    assert pinned is not None

    index = 1
    failed = await service.sync_now(principal, force=True)
    assert failed.status == "failed"
    assert old_entry["sha256"] in store.revoked_digests()
    assert store.current_snapshot().snapshot_id == pinned.snapshot_id

    metadata = {
        IDENTITY_METADATA_KEY: {"managed_skills_path": str(store.root)},
        SKILL_SNAPSHOT_METADATA_KEY: pinned.model_dump(mode="json", by_alias=True),
    }
    with request_context(RequestContext(channel="test", chat_id="1", metadata=metadata)):
        result = await ReadSkillTool().execute("managed-guide")
    assert "has been revoked" in result
    await service.close()


@pytest.mark.asyncio
async def test_non_auto_policy_defaults_to_current_installed_version(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifest = manifest_factory(entries=[entry])
    put_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})
        if request.method == "PUT":
            put_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"rowVersion": 2})
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"

    async def skip_sync(
        principal: Principal,
        skill_key: str,
        remote: dict[str, Any],
        *,
        reason: str,
    ) -> SkillOperationResult:
        return SkillOperationResult(status="applied", skillKey=skill_key, details=remote)

    service._operation_sync = skip_sync  # type: ignore[method-assign]
    for policy in ("manual", "notify", "pinned"):
        await service.set_policy(
            principal,
            "managed-guide",
            update_policy=policy,
            expected_row_version=1,
        )
    await service.set_policy(
        principal,
        "managed-guide",
        update_policy="auto_stable",
        version="must-be-cleared",
        expected_row_version=1,
    )

    assert put_payloads == [
        {"updatePolicy": "manual", "version": "1.0.0", "expectedRowVersion": 1},
        {"updatePolicy": "notify", "version": "1.0.0", "expectedRowVersion": 1},
        {"updatePolicy": "pinned", "version": "1.0.0", "expectedRowVersion": 1},
        {"updatePolicy": "auto_stable", "expectedRowVersion": 1},
    ]
    await service.close()

    empty_service = _service(tmp_path / "empty", signing_material[2], handler)
    with pytest.raises(SkillMarketError, match="Update policy requires a version"):
        await empty_service.set_policy(
            principal,
            "managed-guide",
            update_policy="pinned",
        )
    await empty_service.close()


@pytest.mark.asyncio
async def test_missing_snapshot_refetches_full_manifest_after_unusable_304(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifest = manifest_factory(entries=[entry])
    recovering = False
    recovery_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            if recovering:
                recovery_requests.append(request)
                if len(recovery_requests) == 1:
                    return httpx.Response(304, headers={"ETag": f'"{manifest["revision"]}"'})
            return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"
    store = service._store(principal)
    expected_snapshot_id = store.current_snapshot().snapshot_id
    store.current_path.unlink()
    store.previous_path.unlink(missing_ok=True)
    recovering = True

    recovered = await service.sync_now(principal)

    assert recovered.status == "applied"
    assert recovered.snapshot_id == expected_snapshot_id
    assert len(recovery_requests) == 2
    assert all("if-none-match" not in request.headers for request in recovery_requests)
    await service.close()


@pytest.mark.asyncio
async def test_expired_manifest_with_repeated_304_keeps_recent_lkg_available_offline(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    manifest = manifest_factory(entries=[entry])
    return_not_modified = False
    manifest_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            manifest_requests.append(request)
            if return_not_modified:
                return httpx.Response(304, headers={"ETag": f'"{manifest["revision"]}"'})
            return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, content=artifact, headers=artifact_headers)
        return httpx.Response(503, json={"code": "OFFLINE"})

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"
    service._store(principal).update_sync_state(
        manifestValidUntil=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    )
    return_not_modified = True

    assert (await service.sync_now(principal)).status == "unchanged"
    assert (await service.sync_now(principal)).status == "unchanged"
    inventory = await service.inventory(principal)

    assert all(
        "if-none-match" not in request.headers
        for request in manifest_requests[-2:]
    )
    assert inventory["remoteAvailable"] is False
    assert inventory["local"]["installed"][0]["skillKey"] == "managed-guide"
    assert inventory["local"]["sync"]["manifestExpired"] is True
    assert inventory["local"]["sync"]["lkgStale"] is False
    await service.close()


@pytest.mark.asyncio
async def test_sync_reports_validate_against_backend_strict_dto(
    tmp_path: Path,
    artifact_factory,
    manifest_factory,
    signing_material,
) -> None:
    artifact, entry, artifact_headers = artifact_factory()
    active_manifest = manifest_factory(entries=[entry])
    rejected_manifest = manifest_factory(
        org_id="other-org",
        global_revision=2,
        org_revision=2,
        entries=[entry],
    )
    current_manifest = active_manifest
    reports: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/nanobot/skills/manifest":
            return httpx.Response(
                200,
                json=current_manifest,
                headers={"ETag": f'"{current_manifest["revision"]}"'},
            )
        if request.url.path.endswith("/sync-report"):
            reports.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(200, content=artifact, headers=artifact_headers)

    service = _service(tmp_path, signing_material[2], handler)
    principal = Principal(user_id="user-1", org_id="org-1")
    assert (await service.sync_now(principal, force=True)).status == "applied"
    current_manifest = rejected_manifest
    assert (await service.sync_now(principal, force=True)).status == "failed"
    await service.close()

    report_model: type[Any] = SkillSyncReport
    backend_src = (
        Path(__file__).resolve().parents[3]
        / "kangaroo-ai-agent-v2"
        / "src"
    )
    if backend_src.is_dir():
        sys.path.insert(0, str(backend_src))
        try:
            module = importlib.import_module(
                "kangaroo_ai_agent.nanobot.skill_market.models"
            )
            report_model = module.SkillSyncReportRequest
        finally:
            sys.path.remove(str(backend_src))

    assert {payload["status"] for payload in reports} == {"success", "rejected"}
    assert {payload["eventType"] for payload in reports} == {"sync", "reject"}
    assert len({payload["eventId"] for payload in reports}) == len(reports)
    assert len({payload["correlationId"] for payload in reports}) == len(reports)
    for payload in reports:
        report_model.model_validate(payload)


@pytest.mark.asyncio
async def test_manifest_revision_digest_covers_audience_generation_and_desired_state(
    tmp_path: Path,
    manifest_factory,
    signing_material,
) -> None:
    manifest = manifest_factory(digest="a" * 64)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sync-report"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json=manifest, headers={"ETag": f'"{manifest["revision"]}"'})

    service = _service(tmp_path, signing_material[2], handler)
    result = await service.sync_now(Principal(user_id="user-1", org_id="org-1"), force=True)

    assert result.status == "failed"
    assert result.error_code == "MANIFEST_INVALID"
    await service.close()


@pytest.mark.asyncio
async def test_start_is_awaitable_idempotent_and_allows_same_org_credential_failover(
    tmp_path: Path,
    signing_material,
) -> None:
    service = _service(
        tmp_path,
        signing_material[2],
        lambda request: httpx.Response(500),
    )
    called = asyncio.Event()

    async def fake_sync(principal: Principal, *, force: bool = False) -> SyncResult:
        called.set()
        return SyncResult(status="unchanged", pollAfterSeconds=300)

    service.sync_now = fake_sync  # type: ignore[method-assign]
    first = Principal(user_id="user-1", org_id="org-1")
    second = Principal(user_id="user-2", org_id="org-1")

    await service.start(first)
    await called.wait()
    task = service._poll_tasks[first.org_scope]
    await service.start(second)

    assert service._poll_tasks[first.org_scope] is task
    assert service._poll_principals[first.org_scope] == second
    await service.close()


@pytest.mark.asyncio
async def test_set_pinned_policy_forwards_current_version(
    tmp_path: Path,
    signing_material,
) -> None:
    service = _service(
        tmp_path,
        signing_material[2],
        lambda request: httpx.Response(500),
    )
    principal = Principal(user_id="user-1", org_id="org-1")
    service.client.put_subscription = AsyncMock(return_value={"rowVersion": 5})
    service._operation_sync = AsyncMock(return_value=object())  # type: ignore[method-assign]

    await service.set_policy(
        principal,
        "managed-guide",
        update_policy="pinned",
        version="1.2.3",
        expected_row_version=4,
    )

    service.client.put_subscription.assert_awaited_once_with(
        principal,
        "managed-guide",
        {
            "updatePolicy": "pinned",
            "version": "1.2.3",
            "expectedRowVersion": 4,
        },
    )
    await service.close()
