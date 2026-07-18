import asyncio
from pathlib import Path

import pytest

from nanobot.identity.credentials import KangarooCredentialStore, KangarooInstanceBindingError
from nanobot.identity.kangaroo import (
    AuthenticatedKangarooIdentity,
    KangarooIdentityError,
    KangarooTokenBundle,
)
from nanobot.identity.principal import Principal


def _principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, org_id="org-1")


def test_credentials_are_isolated_and_redacted() -> None:
    store = KangarooCredentialStore()
    first = _principal("user-1")
    second = _principal("user-2")

    store.put(first, "secret-token-one")
    store.put(second, "secret-token-two")

    assert store.get(first.user_scope) == "secret-token-one"
    assert store.get(second.user_scope) == "secret-token-two"
    assert "secret-token" not in repr(store)
    identity = AuthenticatedKangarooIdentity(first, "secret-token-one")
    assert "secret-token-one" not in repr(identity)
    assert "secret-token-one" not in str(first.metadata())


def test_credentials_expire_and_evict_oldest_entry() -> None:
    now = 100.0
    store = KangarooCredentialStore(
        max_entries=1,
        ttl_s=10,
        clock=lambda: now,
    )
    first = _principal("user-1")
    second = _principal("user-2")

    store.put(first, "first-token")
    store.put(second, "second-token")

    assert store.get(first.user_scope) is None
    assert store.get(second.user_scope) == "second-token"

    now = 111.0
    assert store.get(second.user_scope) is None


def test_credentials_survive_restart_without_plaintext_on_disk(tmp_path: Path) -> None:
    principal = _principal("user-1")
    vault_path = tmp_path / "auth" / "credentials.enc"
    first = KangarooCredentialStore(persistence_path=vault_path)
    first.put(
        principal,
        "secret-access",
        refresh_token="secret-refresh",
        expires_at=9_999_999_999,
        refresh_expires_at=9_999_999_999,
    )

    raw = vault_path.read_bytes()
    assert b"secret-access" not in raw
    assert b"secret-refresh" not in raw
    assert vault_path.stat().st_mode & 0o077 == 0

    restored = KangarooCredentialStore(persistence_path=vault_path)
    assert restored.get(principal.user_scope) == "secret-access"


def test_instance_binding_survives_restart_and_clears_with_credentials(tmp_path: Path) -> None:
    principal = _principal("institution-account")
    vault_path = tmp_path / "auth" / "credentials.enc"
    first = KangarooCredentialStore(persistence_path=vault_path)
    first.put(
        principal,
        "institution-access",
        refresh_token="institution-refresh",
        expires_at=9_999_999_999,
    )
    first.bind_instance(principal)

    restored = KangarooCredentialStore(persistence_path=vault_path)

    assert restored.instance_principal() == principal
    metadata = restored.instance_identity_metadata()
    assert metadata is not None
    assert metadata["user_scope"] == principal.user_scope
    assert metadata["org_scope"] == principal.org_scope
    assert metadata["user_memory_path"].endswith("/workspace/memory/MEMORY.md")
    assert metadata["org_memory_path"].endswith("/memory/MEMORY.md")
    assert b"institution-account" not in vault_path.read_bytes()
    assert restored.remove(principal.user_scope) is True
    assert restored.instance_principal() is None


def test_instance_binding_cannot_be_replaced_by_another_account() -> None:
    store = KangarooCredentialStore()
    first = _principal("institution-one")
    second = _principal("institution-two")
    store.put_instance(first, "first-access")

    with pytest.raises(KangarooInstanceBindingError, match="already bound"):
        store.put_instance(second, "second-access")

    assert store.instance_principal() == first
    assert store.get(second.user_scope) is None


@pytest.mark.asyncio
async def test_proactive_refresh_is_singleflight_for_concurrent_requests() -> None:
    now = 100.0
    calls = 0

    async def refresh(refresh_token: str) -> KangarooTokenBundle:
        nonlocal calls
        calls += 1
        assert refresh_token == "old-refresh"
        await asyncio.sleep(0)
        return KangarooTokenBundle(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at=1000,
        )

    principal = _principal("user-1")
    store = KangarooCredentialStore(
        clock=lambda: now,
        refresh_skew_s=20,
        refresher=refresh,
    )
    store.put(
        principal,
        "old-access",
        refresh_token="old-refresh",
        expires_at=110,
    )

    tokens = await asyncio.gather(
        store.get_valid_access_token(principal.user_scope),
        store.get_valid_access_token(principal.user_scope),
        store.get_valid_access_token(principal.user_scope),
    )

    assert tokens == ["new-access", "new-access", "new-access"]
    assert calls == 1


@pytest.mark.asyncio
async def test_401_refresh_preserves_a_token_rotated_by_another_request() -> None:
    calls = 0

    async def refresh(_: str) -> KangarooTokenBundle:
        nonlocal calls
        calls += 1
        return KangarooTokenBundle("unexpected")

    principal = _principal("user-1")
    store = KangarooCredentialStore(refresher=refresh)
    store.put(principal, "new-access", refresh_token="new-refresh")

    token = await store.refresh_access_token(
        principal.user_scope,
        rejected_access_token="old-access",
    )

    assert token == "new-access"
    assert calls == 0


@pytest.mark.asyncio
async def test_transient_refresh_failure_keeps_persisted_credentials() -> None:
    async def refresh(_: str) -> KangarooTokenBundle:
        raise KangarooIdentityError("temporarily unavailable", http_status=503)

    principal = _principal("user-1")
    store = KangarooCredentialStore(
        clock=lambda: 100,
        refresh_skew_s=20,
        refresher=refresh,
    )
    store.put(
        principal,
        "old-access",
        refresh_token="old-refresh",
        expires_at=110,
    )

    with pytest.raises(KangarooIdentityError, match="temporarily unavailable"):
        await store.get_valid_access_token(principal.user_scope)

    assert store.get(principal.user_scope) == "old-access"


@pytest.mark.asyncio
async def test_invalid_refresh_token_clears_persisted_credentials() -> None:
    async def refresh(_: str) -> KangarooTokenBundle:
        raise KangarooIdentityError("invalid refresh token", http_status=401)

    principal = _principal("user-1")
    store = KangarooCredentialStore(refresher=refresh)
    store.put(principal, "old-access", refresh_token="old-refresh")

    with pytest.raises(KangarooIdentityError, match="invalid refresh token"):
        await store.refresh_access_token(
            principal.user_scope,
            rejected_access_token="old-access",
        )

    assert store.get(principal.user_scope) is None
