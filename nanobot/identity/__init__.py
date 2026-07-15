"""Authenticated identities used by the multi-tenant gateway."""

from nanobot.identity.credentials import KangarooCredentialStore, get_kangaroo_credential_store
from nanobot.identity.kangaroo import (
    AuthenticatedKangarooIdentity,
    KangarooIdentityError,
    KangarooIdentityVerifier,
    KangarooTokenBundle,
)
from nanobot.identity.principal import Principal

__all__ = [
    "AuthenticatedKangarooIdentity",
    "KangarooCredentialStore",
    "KangarooIdentityError",
    "KangarooIdentityVerifier",
    "KangarooTokenBundle",
    "Principal",
    "get_kangaroo_credential_store",
]
