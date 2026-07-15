"""Token state for the embedded WebUI gateway."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from websockets.http11 import Request as WsRequest

from nanobot.identity.principal import Principal
from nanobot.webui.http_utils import bearer_token, parse_query, query_first


@dataclass(frozen=True, slots=True)
class TokenGrant:
    expires_at: float
    principal: Principal | None = None


@dataclass
class GatewayTokenStore:
    """Own short-lived WebSocket and WebUI API tokens for one gateway process."""

    max_tokens: int = 10_000
    issued_tokens: dict[str, float | TokenGrant] = field(default_factory=dict)
    api_tokens: dict[str, float | TokenGrant] = field(default_factory=dict)

    def check_api_token(self, request: WsRequest) -> bool:
        self._purge_expired_api_tokens()
        token = bearer_token(request.headers) or query_first(
            parse_query(request.path), "token"
        )
        if not token:
            return False
        grant = self._as_grant(self.api_tokens.get(token))
        if grant is None or time.monotonic() > grant.expires_at:
            self.api_tokens.pop(token, None)
            return False
        return True

    def principal_for_api_request(self, request: WsRequest) -> Principal | None:
        self._purge_expired_api_tokens()
        token = bearer_token(request.headers) or query_first(parse_query(request.path), "token")
        grant = self._as_grant(self.api_tokens.get(token or ""))
        return grant.principal if grant is not None else None

    def can_issue(self, *, include_api_token: bool = False) -> bool:
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if len(self.issued_tokens) >= self.max_tokens:
            return False
        if include_api_token and len(self.api_tokens) >= self.max_tokens:
            return False
        return True

    def issue_token(self, ttl_s: int | float, principal: Principal | None = None) -> str:
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.issued_tokens[token_value] = TokenGrant(expiry, principal)
        return token_value

    def issue_api_token(self, ttl_s: int | float, principal: Principal | None = None) -> str:
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.api_tokens[token_value] = TokenGrant(expiry, principal)
        return token_value

    def take_issued_token_if_valid(self, token_value: str | None) -> bool:
        return self.take_issued_grant_if_valid(token_value) is not None

    def take_issued_grant_if_valid(self, token_value: str | None) -> TokenGrant | None:
        if not token_value:
            return None
        self._purge_expired_issued_tokens()
        grant = self._as_grant(self.issued_tokens.pop(token_value, None))
        if grant is None or time.monotonic() > grant.expires_at:
            return None
        return grant

    def clear(self) -> None:
        self.issued_tokens.clear()
        self.api_tokens.clear()

    def revoke_principal(self, principal: Principal) -> None:
        """Revoke every outstanding browser and WebSocket grant for an identity."""
        for store in (self.issued_tokens, self.api_tokens):
            for token_key, value in list(store.items()):
                grant = self._as_grant(value)
                if grant is not None and grant.principal == principal:
                    store.pop(token_key, None)

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, value in list(self.api_tokens.items()):
            grant = self._as_grant(value)
            if grant is None or now > grant.expires_at:
                self.api_tokens.pop(token_key, None)

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, value in list(self.issued_tokens.items()):
            grant = self._as_grant(value)
            if grant is None or now > grant.expires_at:
                self.issued_tokens.pop(token_key, None)

    @staticmethod
    def _as_grant(value: float | TokenGrant | None) -> TokenGrant | None:
        # Float support preserves compatibility with callers that seed stores in tests.
        if isinstance(value, TokenGrant):
            return value
        if isinstance(value, (int, float)):
            return TokenGrant(float(value))
        return None
