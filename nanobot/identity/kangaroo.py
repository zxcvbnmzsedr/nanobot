"""Validation of Kangaroo access tokens against the account service."""

from __future__ import annotations

import hashlib
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


class KangarooIdentityVerifier:
    def __init__(
        self,
        *,
        api_base: str,
        user_info_path: str,
        login_path: str = "api/auth/oauth/login",
        timeout_s: float,
        allowed_user_ids: set[str] | None = None,
    ) -> None:
        base = api_base.strip().rstrip("/") + "/"
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("kangaroo api_base must be an absolute HTTP(S) URL")
        self.user_info_url = self._same_host_url(base, user_info_path, parsed.netloc)
        self.login_url = self._same_host_url(base, login_path, parsed.netloc)
        self.timeout_s = timeout_s
        self.allowed_user_ids = allowed_user_ids or set()

    @staticmethod
    def _same_host_url(base: str, path: str, expected_host: str) -> str:
        url = urljoin(base, path.lstrip("/"))
        if urlparse(url).netloc != expected_host:
            raise ValueError("kangaroo auth paths must stay on api_base host")
        return url

    async def login(self, username: str, password: str) -> Principal:
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
        access_token = token_payload.get("value") if isinstance(token_payload, dict) else None
        if not isinstance(access_token, str) or not access_token.strip():
            raise KangarooIdentityError("登录接口未返回有效凭证。")
        return await self.verify(access_token)

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
            raise KangarooIdentityError("账号或密码错误。")
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
                    else "账号或密码错误。"
                )
        return body
