"""Refreshable, encrypted storage for Kangaroo OAuth credentials."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from nanobot.identity.kangaroo import (
    AuthenticatedKangarooIdentity,
    KangarooIdentityError,
    KangarooTokenBundle,
)
from nanobot.identity.principal import Principal

_VAULT_VERSION = 1
_KEY_ENV = "NANOBOT_KANGAROO_CREDENTIAL_KEY"


@dataclass(frozen=True, slots=True)
class _Credential:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    refresh_expires_at: float | None = None


RefreshCallback = Callable[[str], Awaitable[KangarooTokenBundle]]


class KangarooInstanceBindingError(RuntimeError):
    """Raised when another account tries to replace the instance owner."""


class KangarooCredentialStore:
    """Per-user OAuth credentials with refresh, rotation, and restart recovery."""

    def __init__(
        self,
        *,
        max_entries: int = 2048,
        ttl_s: float = 86_400,
        refresh_skew_s: float = 3_600,
        clock: Callable[[], float] = time.time,
        persistence_path: Path | None = None,
        refresher: RefreshCallback | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if refresh_skew_s < 0:
            raise ValueError("refresh_skew_s must not be negative")
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._refresh_skew_s = refresh_skew_s
        self._clock = clock
        self._persistence_path = persistence_path
        self._refresher = refresher
        self._entries: OrderedDict[str, _Credential] = OrderedDict()
        self._instance_principal: Principal | None = None
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._lock = threading.RLock()
        if persistence_path is not None:
            self._load()

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"{type(self).__name__}(entries={len(self._entries)}, "
                f"max_entries={self._max_entries}, persistent={self._persistence_path is not None})"
            )

    def configure(
        self,
        *,
        persistence_path: Path,
        refresher: RefreshCallback,
        refresh_skew_s: float,
    ) -> None:
        """Bind the process-wide store to the active gateway instance."""
        resolved = persistence_path.expanduser().resolve(strict=False)
        with self._lock:
            path_changed = self._persistence_path != resolved
            self._persistence_path = resolved
            self._refresher = refresher
            self._refresh_skew_s = refresh_skew_s
            if path_changed:
                self._entries.clear()
                self._instance_principal = None
                self._refresh_locks.clear()
                self._fernet()
                self._load_locked()

    def put(
        self,
        principal: Principal,
        access_token: str,
        *,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        refresh_expires_at: float | None = None,
    ) -> None:
        token = access_token.strip()
        if not token:
            raise ValueError("access_token must not be empty")
        clean_refresh = refresh_token.strip() if refresh_token and refresh_token.strip() else None
        now = self._clock()
        credential = _Credential(
            access_token=token,
            refresh_token=clean_refresh,
            expires_at=expires_at if expires_at is not None else now + self._ttl_s,
            refresh_expires_at=refresh_expires_at,
        )
        with self._lock:
            self._purge_expired_refresh_tokens_locked(now)
            self._entries.pop(principal.user_scope, None)
            self._entries[principal.user_scope] = credential
            while len(self._entries) > self._max_entries:
                evicted_scope, _ = self._entries.popitem(last=False)
                self._clear_instance_binding_locked(evicted_scope)
            self._save_locked()

    def put_identity(self, identity: AuthenticatedKangarooIdentity) -> None:
        self.put(
            identity.principal,
            identity.access_token,
            refresh_token=identity.refresh_token,
            expires_at=identity.expires_at,
            refresh_expires_at=identity.refresh_expires_at,
        )

    def put_instance_identity(self, identity: AuthenticatedKangarooIdentity) -> None:
        """Store credentials and bind the verified account to this instance."""
        with self._lock:
            self._ensure_instance_binding_allowed_locked(identity.principal)
            self.put_identity(identity)
            self._instance_principal = identity.principal
            self._save_locked()

    def put_instance(self, principal: Principal, access_token: str) -> None:
        """Store an exchanged token and bind its account to this instance."""
        with self._lock:
            self._ensure_instance_binding_allowed_locked(principal)
            self.put(principal, access_token)
            self._instance_principal = principal
            self._save_locked()

    def bind_instance(self, principal: Principal) -> None:
        """Use a verified account as the identity for instance-owned channels."""
        with self._lock:
            if principal.user_scope not in self._entries:
                raise ValueError("instance identity requires stored Kangaroo credentials")
            self._ensure_instance_binding_allowed_locked(principal)
            self._instance_principal = principal
            self._save_locked()

    def instance_principal(self) -> Principal | None:
        """Return the verified account bound to this nanobot instance."""
        with self._lock:
            principal = self._instance_principal
            if principal is None or principal.user_scope not in self._entries:
                return None
            return principal

    def instance_identity_metadata(self) -> dict[str, Any] | None:
        """Return instance identity plus durable memory paths when configured."""
        with self._lock:
            principal = self._instance_principal
            persistence_path = self._persistence_path
            if principal is None or principal.user_scope not in self._entries:
                return None
        if persistence_path is None:
            return principal.metadata()

        from nanobot.identity.runtime import TenantRuntimeStore

        runtime = TenantRuntimeStore(persistence_path.parent).for_principal(principal)
        return runtime.identity_metadata()

    def get(self, user_scope: str) -> str | None:
        """Return the cached access token without performing network I/O."""
        now = self._clock()
        with self._lock:
            credential = self._entries.get(user_scope)
            if credential is None:
                return None
            if self._access_expired(credential, now) and not self._refresh_available(
                credential, now
            ):
                self._entries.pop(user_scope, None)
                self._clear_instance_binding_locked(user_scope)
                self._save_locked()
                return None
            self._entries.move_to_end(user_scope)
            return credential.access_token

    async def get_valid_access_token(self, user_scope: str) -> str | None:
        """Return a usable token, refreshing once when it is near expiry."""
        credential = self._credential(user_scope)
        if credential is None:
            return None
        now = self._clock()
        if not self._refresh_due(credential, now):
            return credential.access_token
        if not self._refresh_available(credential, now):
            if self._access_expired(credential, now):
                self.remove(user_scope, access_token=credential.access_token)
                return None
            return credential.access_token
        return await self._refresh(
            user_scope,
            expected_access_token=credential.access_token,
            remove_if_unrefreshable=False,
        )

    async def refresh_access_token(
        self,
        user_scope: str,
        *,
        rejected_access_token: str,
    ) -> str | None:
        """Refresh after a 401, unless another request already rotated the token."""
        return await self._refresh(
            user_scope,
            expected_access_token=rejected_access_token,
            remove_if_unrefreshable=True,
        )

    def remove(self, user_scope: str, *, access_token: str | None = None) -> bool:
        with self._lock:
            credential = self._entries.get(user_scope)
            if credential is None:
                binding_removed = self._clear_instance_binding_locked(user_scope)
                if binding_removed:
                    self._save_locked()
                return binding_removed
            if access_token is not None and credential.access_token != access_token:
                return False
            self._entries.pop(user_scope, None)
            self._clear_instance_binding_locked(user_scope)
            self._refresh_locks.pop(user_scope, None)
            self._save_locked()
            return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._instance_principal = None
            self._refresh_locks.clear()
            self._save_locked()

    async def _refresh(
        self,
        user_scope: str,
        *,
        expected_access_token: str,
        remove_if_unrefreshable: bool,
    ) -> str | None:
        lock = self._refresh_lock(user_scope)
        async with lock:
            credential = self._credential(user_scope)
            if credential is None:
                return None
            if credential.access_token != expected_access_token:
                return credential.access_token

            now = self._clock()
            if not self._refresh_available(credential, now) or self._refresher is None:
                if remove_if_unrefreshable or self._access_expired(credential, now):
                    self.remove(user_scope, access_token=credential.access_token)
                    return None
                return credential.access_token

            try:
                bundle = await self._refresher(credential.refresh_token or "")
            except KangarooIdentityError as exc:
                if exc.http_status < 500:
                    self.remove(user_scope, access_token=credential.access_token)
                raise

            latest = self._credential(user_scope)
            if latest is None:
                return None
            if latest.access_token != credential.access_token:
                return latest.access_token
            self._put_refreshed(user_scope, latest, bundle)
            return bundle.access_token

    def _put_refreshed(
        self,
        user_scope: str,
        previous: _Credential,
        bundle: KangarooTokenBundle,
    ) -> None:
        now = self._clock()
        refresh_token = bundle.refresh_token or previous.refresh_token
        refresh_expires_at = bundle.refresh_expires_at
        if refresh_expires_at is None and refresh_token == previous.refresh_token:
            refresh_expires_at = previous.refresh_expires_at
        credential = _Credential(
            access_token=bundle.access_token,
            refresh_token=refresh_token,
            expires_at=bundle.expires_at if bundle.expires_at is not None else now + self._ttl_s,
            refresh_expires_at=refresh_expires_at,
        )
        with self._lock:
            self._entries.pop(user_scope, None)
            self._entries[user_scope] = credential
            self._save_locked()

    def _credential(self, user_scope: str) -> _Credential | None:
        with self._lock:
            credential = self._entries.get(user_scope)
            if credential is not None:
                self._entries.move_to_end(user_scope)
            return credential

    def _refresh_lock(self, user_scope: str) -> asyncio.Lock:
        with self._lock:
            return self._refresh_locks.setdefault(user_scope, asyncio.Lock())

    def _refresh_due(self, credential: _Credential, now: float) -> bool:
        return (
            credential.expires_at is not None
            and credential.expires_at <= now + self._refresh_skew_s
        )

    @staticmethod
    def _access_expired(credential: _Credential, now: float) -> bool:
        return credential.expires_at is not None and credential.expires_at <= now

    @staticmethod
    def _refresh_available(credential: _Credential, now: float) -> bool:
        return bool(credential.refresh_token) and (
            credential.refresh_expires_at is None or credential.refresh_expires_at > now
        )

    def _purge_expired_refresh_tokens_locked(self, now: float) -> None:
        expired = [
            user_scope
            for user_scope, credential in self._entries.items()
            if self._access_expired(credential, now)
            and not self._refresh_available(credential, now)
        ]
        for user_scope in expired:
            self._entries.pop(user_scope, None)
            self._clear_instance_binding_locked(user_scope)

    def _clear_instance_binding_locked(self, user_scope: str) -> bool:
        principal = self._instance_principal
        if principal is None or principal.user_scope != user_scope:
            return False
        self._instance_principal = None
        return True

    def _ensure_instance_binding_allowed_locked(self, principal: Principal) -> None:
        current = self._instance_principal
        if current is None:
            return
        if (
            current.user_scope != principal.user_scope
            or current.org_scope != principal.org_scope
        ):
            raise KangarooInstanceBindingError(
                "nanobot instance is already bound to another Kangaroo account"
            )

    def _load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        path = self._persistence_path
        if path is None or not path.exists():
            return
        try:
            decrypted = self._fernet().decrypt(path.read_bytes())
            payload = json.loads(decrypted.decode("utf-8"))
            if payload.get("version") != _VAULT_VERSION:
                raise ValueError("unsupported credential vault version")
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                raise ValueError("credential vault entries must be an object")
            now = self._clock()
            for user_scope, raw in entries.items():
                credential = self._credential_from_json(raw)
                if not isinstance(user_scope, str) or credential is None:
                    continue
                if self._access_expired(credential, now) and not self._refresh_available(
                    credential, now
                ):
                    continue
                self._entries[user_scope] = credential
            principal = self._principal_from_json(payload.get("instancePrincipal"))
            if principal is not None and principal.user_scope in self._entries:
                self._instance_principal = principal
        except (OSError, ValueError, InvalidToken, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable Kangaroo credential vault: {}", exc)

    def _save_locked(self) -> None:
        path = self._persistence_path
        if path is None:
            return
        payload = {
            "version": _VAULT_VERSION,
            "entries": {
                user_scope: {
                    "accessToken": credential.access_token,
                    "refreshToken": credential.refresh_token,
                    "expiresAt": credential.expires_at,
                    "refreshExpiresAt": credential.refresh_expires_at,
                }
                for user_scope, credential in self._entries.items()
            },
            "instancePrincipal": (
                self._instance_principal.public_payload()
                if self._instance_principal is not None
                else None
            ),
        }
        encrypted = self._fernet().encrypt(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        )
        self._atomic_write(path, encrypted)

    def _fernet(self) -> Fernet:
        configured = os.environ.get(_KEY_ENV, "").strip()
        if configured:
            try:
                return Fernet(configured.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError(f"{_KEY_ENV} is not a valid Fernet key") from exc

        path = self._persistence_path
        if path is None:
            raise RuntimeError("credential persistence is not configured")
        key_path = path.with_suffix(path.suffix + ".key")
        if not key_path.exists():
            self._atomic_write(key_path, Fernet.generate_key())
        with suppress(OSError):
            key_path.chmod(0o600)
        return Fernet(key_path.read_bytes().strip())

    @staticmethod
    def _credential_from_json(raw: Any) -> _Credential | None:
        if not isinstance(raw, dict):
            return None
        access_token = raw.get("accessToken")
        if not isinstance(access_token, str) or not access_token.strip():
            return None
        refresh_token = raw.get("refreshToken")
        return _Credential(
            access_token=access_token.strip(),
            refresh_token=(
                refresh_token.strip()
                if isinstance(refresh_token, str) and refresh_token.strip()
                else None
            ),
            expires_at=KangarooCredentialStore._optional_timestamp(raw.get("expiresAt")),
            refresh_expires_at=KangarooCredentialStore._optional_timestamp(
                raw.get("refreshExpiresAt")
            ),
        )

    @staticmethod
    def _principal_from_json(raw: Any) -> Principal | None:
        if not isinstance(raw, dict):
            return None
        try:
            return Principal.from_kangaroo_payload({
                "id": raw.get("userId"),
                "orgId": raw.get("orgId"),
                "name": raw.get("name"),
                "orgName": raw.get("orgName"),
                "orgType": raw.get("orgType"),
                "accType": raw.get("accType"),
            })
        except ValueError:
            return None

    @staticmethod
    def _optional_timestamp(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "wb") as handle:
                with suppress(OSError):
                    os.chmod(tmp_path, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            with suppress(OSError):
                path.chmod(0o600)
            with suppress(OSError):
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


_KANGAROO_CREDENTIAL_STORE = KangarooCredentialStore()


def get_kangaroo_credential_store() -> KangarooCredentialStore:
    """Return the process-wide credential store shared by auth and providers."""
    return _KANGAROO_CREDENTIAL_STORE
