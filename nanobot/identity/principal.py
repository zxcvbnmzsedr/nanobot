"""Trusted account identity attached to gateway credentials and messages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

IDENTITY_METADATA_KEY = "nanobot_identity"
_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


def _normalize_id(value: Any, field: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a string or integer")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or integer")
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field} has an invalid format")
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity asserted by a trusted upstream account service."""

    user_id: str
    org_id: str
    name: str = ""
    org_name: str = ""
    org_type: int | None = None
    account_type: int | None = None
    permissions: tuple[str, ...] = ("memory:user:read", "memory:user:write", "memory:org:read")
    source: str = "kangaroo"

    @classmethod
    def from_kangaroo_payload(cls, payload: dict[str, Any]) -> "Principal":
        return cls(
            user_id=_normalize_id(payload.get("id"), "user id"),
            org_id=_normalize_id(payload.get("orgId"), "organization id"),
            name=str(payload.get("name") or "").strip()[:128],
            org_name=str(payload.get("orgName") or "").strip()[:128],
            org_type=payload.get("orgType") if isinstance(payload.get("orgType"), int) else None,
            account_type=payload.get("accType") if isinstance(payload.get("accType"), int) else None,
        )

    @property
    def user_scope(self) -> str:
        """Filesystem-safe, non-enumerable directory key for this user."""
        return hashlib.sha256(f"{self.source}:user:{self.user_id}".encode()).hexdigest()[:24]

    @property
    def org_scope(self) -> str:
        """Filesystem-safe, non-enumerable directory key for this organization."""
        return hashlib.sha256(f"{self.source}:org:{self.org_id}".encode()).hexdigest()[:24]

    def metadata(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "name": self.name,
            "org_name": self.org_name,
            "org_type": self.org_type,
            "account_type": self.account_type,
            "permissions": list(self.permissions),
            "user_scope": self.user_scope,
            "org_scope": self.org_scope,
        }

    def public_payload(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "orgId": self.org_id,
            "name": self.name,
            "orgName": self.org_name,
            "orgType": self.org_type,
            "accType": self.account_type,
        }
