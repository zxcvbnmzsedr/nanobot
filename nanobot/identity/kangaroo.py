"""Validation of Kangaroo access tokens against the account service."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from nanobot.identity.principal import Principal

_OAUTH_BASIC_AUTHORIZATION = (
    "Basic a2FuZ2Fyb28taW50ZWxsaWdlbnQtd2ViOng2UjZib3JWS0pSSkFrQ0s="
)
_HISTORY_PASSWORD_SUFFIX = "!eSa36J5s!gdYQPF"


class KangarooIdentityError(RuntimeError):
    """Raised when an upstream token cannot be exchanged for a trusted identity."""

    def __init__(self, message: str, *, http_status: int = 401) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class KangarooTokenBundle:
    """Upstream OAuth tokens and their absolute wall-clock expiry times."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    refresh_expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedKangarooIdentity:
    """A verified principal paired with the access token used to verify it."""

    principal: Principal
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    refresh_expires_at: float | None = None

    def token_bundle(self) -> KangarooTokenBundle:
        return KangarooTokenBundle(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at,
            refresh_expires_at=self.refresh_expires_at,
        )


class KangarooIdentityVerifier:
    def __init__(
        self,
        *,
        api_base: str,
        user_info_path: str,
        login_path: str = "api/auth/oauth/login",
        refresh_path: str = "api/auth/oauth/token",
        timeout_s: float,
        allowed_user_ids: set[str] | None = None,
    ) -> None:
        base = api_base.strip().rstrip("/") + "/"
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("kangaroo api_base must be an absolute HTTP(S) URL")
        self.user_info_url = self._same_host_url(base, user_info_path, parsed.netloc)
        self.login_url = self._same_host_url(base, login_path, parsed.netloc)
        self.refresh_url = self._same_host_url(base, refresh_path, parsed.netloc)
        self.timeout_s = timeout_s
        self.allowed_user_ids = allowed_user_ids or set()

    @staticmethod
    def _same_host_url(base: str, path: str, expected_host: str) -> str:
        url = urljoin(base, path.lstrip("/"))
        if urlparse(url).netloc != expected_host:
            raise ValueError("kangaroo auth paths must stay on api_base host")
        return url

    async def login_with_access_token(
        self,
        username: str,
        password: str,
    ) -> AuthenticatedKangarooIdentity:
        phone = username.strip()
        if not phone or len(phone) > 128:
            raise KangarooIdentityError("请输入账号或手机号。")
        if not password or len(password) > 256:
            raise KangarooIdentityError("请输入有效密码。")

        encoded_password = hashlib.md5(  # noqa: S324 - required by the upstream contract
            f"{password}{_HISTORY_PASSWORD_SUFFIX}".encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        body = await self._request_json(
            "POST",
            self.login_url,
            headers={"Authorization": _OAUTH_BASIC_AUTHORIZATION},
            json={
                "phone": phone,
                "password": encoded_password,
                "type": "kangaroo-intelligent-web",
                "captchaKey": "",
                "captchaValue": "",
                "grant_type": "password",
                "scope": "all",
            },
        )
        token_payload = body.get("data") if isinstance(body, dict) else None
        bundle = self._login_token_bundle(token_payload)
        principal = await self.verify(bundle.access_token)
        return AuthenticatedKangarooIdentity(
            principal=principal,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            expires_at=bundle.expires_at,
            refresh_expires_at=bundle.refresh_expires_at,
        )

    async def login(self, username: str, password: str) -> Principal:
        """Authenticate and return the trusted principal."""
        return (await self.login_with_access_token(username, password)).principal

    async def refresh(self, refresh_token: str) -> KangarooTokenBundle:
        """Rotate an upstream refresh token and return the next token bundle."""
        token = refresh_token.strip()
        if not token:
            raise KangarooIdentityError("missing Kangaroo refresh token")
        body = await self._request_json(
            "POST",
            self.refresh_url,
            headers={"Authorization": _OAUTH_BASIC_AUTHORIZATION},
            json={
                "refresh_token": token,
                "grant_type": "refresh_token",
            },
            rejected_message="袋鼠登录已过期，请重新登录。",
        )
        token_payload = body.get("data") if isinstance(body, dict) else None
        if not isinstance(token_payload, dict):
            raise KangarooIdentityError(
                "袋鼠刷新接口未返回有效凭证。",
                http_status=502,
            )
        access_token = self._clean_token(token_payload.get("access_token"))
        if access_token is None:
            raise KangarooIdentityError(
                "袋鼠刷新接口未返回 access token。",
                http_status=502,
            )
        return KangarooTokenBundle(
            access_token=access_token,
            refresh_token=self._clean_token(token_payload.get("refresh_token")) or token,
            expires_at=self._resolve_expires_at(None, token_payload.get("expires_in")),
        )

    async def verify(self, access_token: str) -> Principal:
        token = access_token.strip()
        if not token:
            raise KangarooIdentityError("missing Kangaroo access token")
        body = await self._request_json(
            "GET",
            self.user_info_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = body.get("data") if isinstance(body, dict) else None
        if not isinstance(payload, dict):
            raise KangarooIdentityError("Kangaroo account response is missing user data")
        try:
            principal = Principal.from_kangaroo_payload(payload)
        except ValueError as exc:
            raise KangarooIdentityError(str(exc)) from exc
        if self.allowed_user_ids and principal.user_id not in self.allowed_user_ids:
            raise KangarooIdentityError("This Kangaroo account is not allowed to use nanobot")
        return principal

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        rejected_message: str = "账号或密码错误。",
    ) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False) as client:
                response = await client.request(
                    method,
                    url,
                    headers={**headers, "Accept": "application/json"},
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise KangarooIdentityError(
                "袋鼠账号服务暂时不可用，请稍后重试。",
                http_status=503,
            ) from exc
        if response.status_code in {401, 403}:
            raise KangarooIdentityError(rejected_message)
        if response.status_code != 200:
            raise KangarooIdentityError(
                "袋鼠账号服务返回异常，请稍后重试。",
                http_status=502,
            )
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise KangarooIdentityError(
                "袋鼠账号服务返回了无效数据。",
                http_status=502,
            ) from exc
        if isinstance(body, dict):
            err_code = body.get("errCode")
            if isinstance(err_code, int) and err_code != 0:
                message = body.get("msg")
                raise KangarooIdentityError(
                    message.strip()[:200]
                    if isinstance(message, str) and message.strip()
                    else rejected_message
                )
        return body

    @classmethod
    def _login_token_bundle(cls, payload: Any) -> KangarooTokenBundle:
        if not isinstance(payload, dict):
            raise KangarooIdentityError("登录接口未返回有效凭证。")
        access_token = cls._clean_token(payload.get("value"))
        if access_token is None:
            raise KangarooIdentityError("登录接口未返回有效凭证。")
        refresh_payload = payload.get("refreshToken")
        refresh_token = (
            cls._clean_token(refresh_payload.get("value"))
            if isinstance(refresh_payload, dict)
            else None
        )
        return KangarooTokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=cls._resolve_expires_at(
                payload.get("expiration"),
                payload.get("expiresIn"),
            ),
            refresh_expires_at=cls._resolve_expires_at(
                refresh_payload.get("expiration") if isinstance(refresh_payload, dict) else None,
                refresh_payload.get("expiresIn") if isinstance(refresh_payload, dict) else None,
            ),
        )

    @staticmethod
    def _clean_token(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _resolve_expires_at(expiration: Any, expires_in: Any) -> float | None:
        if isinstance(expiration, (int, float)) and expiration > 0:
            value = float(expiration)
            if value > 1_000_000_000_000:
                return value / 1000
            if value > 1_000_000_000:
                return value
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            return time.time() + float(expires_in)
        return None
