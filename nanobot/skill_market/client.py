"""Authenticated HTTP client for the Kangaroo Skill control plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from nanobot.identity.principal import Principal
from nanobot.skill_market.errors import SkillMarketError
from nanobot.skill_market.settings import SkillMarketSettings


class CredentialStore(Protocol):
    async def get_valid_access_token(self, user_scope: str) -> str | None: ...

    async def refresh_access_token(
        self,
        user_scope: str,
        *,
        rejected_access_token: str,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ManifestResponse:
    status_code: int
    payload: dict[str, Any] | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class ArtifactResponse:
    content: bytes
    headers: Mapping[str, str]


class SkillMarketClient:
    def __init__(
        self,
        settings: SkillMarketSettings,
        credential_store: CredentialStore,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.credential_store = credential_store
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=settings.request_timeout_s,
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise SkillMarketError("ARTIFACT_URL_INVALID", "Control-plane path is not same-origin")
        if not parsed.path.startswith("/") or ".." in parsed.path.split("/"):
            raise SkillMarketError("ARTIFACT_URL_INVALID", "Control-plane path is invalid")
        origin = urlsplit(self.settings.base_url)
        query_string = parsed.query
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            query_string = urlencode(clean)
        return urlunsplit((origin.scheme, origin.netloc, parsed.path, query_string, ""))

    async def _request(
        self,
        principal: Principal,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        token = await self.credential_store.get_valid_access_token(principal.user_scope)
        if not token:
            raise SkillMarketError(
                "UNAUTHENTICATED",
                "Kangaroo login is required",
                http_status=401,
            )
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        request_headers["Authorization"] = f"Bearer {token}"

        async def send(access_token: str) -> httpx.Response:
            request_headers["Authorization"] = f"Bearer {access_token}"
            try:
                return await self._client.request(
                    method,
                    self._url(path, query),
                    headers=request_headers,
                    json=dict(json_body) if json_body is not None else None,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise SkillMarketError(
                    "CONTROL_PLANE_UNAVAILABLE",
                    "Skill control plane is unavailable",
                    http_status=503,
                    retryable=True,
                ) from exc

        response = await send(token)
        if response.status_code == 401:
            refreshed = await self.credential_store.refresh_access_token(
                principal.user_scope,
                rejected_access_token=token,
            )
            if refreshed and refreshed != token:
                response = await send(refreshed)
        return response

    @staticmethod
    def _error_from_response(response: httpx.Response) -> SkillMarketError:
        code = "CONTROL_PLANE_ERROR"
        message = "Skill control-plane request failed"
        details: dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if isinstance(payload.get("code"), str):
                code = payload["code"]
            if isinstance(payload.get("message"), str):
                message = payload["message"]
            if isinstance(payload.get("details"), dict):
                details = payload["details"]
        if response.status_code == 401:
            code, message = "UNAUTHENTICATED", "Kangaroo login has expired"
        elif response.status_code == 403:
            code, message = "PERMISSION_DENIED", "Skill marketplace permission denied"
        return SkillMarketError(
            code,
            message,
            http_status=response.status_code,
            retryable=response.status_code >= 500 or response.status_code == 429,
            details=details,
        )

    @classmethod
    def _json_object(cls, response: httpx.Response) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise cls._error_from_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillMarketError(
                "CONTROL_PLANE_INVALID_RESPONSE",
                "Skill control plane returned invalid JSON",
                http_status=502,
                retryable=True,
            ) from exc
        if not isinstance(payload, dict):
            raise SkillMarketError(
                "CONTROL_PLANE_INVALID_RESPONSE",
                "Skill control plane returned an invalid response",
                http_status=502,
                retryable=True,
            )
        return payload

    async def catalog(
        self,
        principal: Principal,
        *,
        query_text: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        response = await self._request(
            principal,
            "GET",
            "/nanobot/skill-market",
            query={"query": query_text, "category": category, "page": page, "pageSize": page_size},
        )
        return self._json_object(response)

    async def detail(self, principal: Principal, skill_key: str) -> dict[str, Any]:
        response = await self._request(
            principal,
            "GET",
            f"/nanobot/skill-market/{quote(skill_key, safe='')}",
        )
        return self._json_object(response)

    async def subscriptions(self, principal: Principal) -> dict[str, Any]:
        response = await self._request(principal, "GET", "/nanobot/my-skills")
        return self._json_object(response)

    async def put_subscription(
        self,
        principal: Principal,
        skill_key: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            principal,
            "PUT",
            f"/nanobot/my-skills/{quote(skill_key, safe='')}",
            json_body=body,
        )
        return self._json_object(response)

    async def delete_subscription(
        self,
        principal: Principal,
        skill_key: str,
        *,
        expected_row_version: str | int | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            principal,
            "DELETE",
            f"/nanobot/my-skills/{quote(skill_key, safe='')}",
            query={"expectedRowVersion": expected_row_version},
        )
        if response.status_code == 204:
            return {}
        return self._json_object(response)

    async def manifest(
        self,
        principal: Principal,
        *,
        etag: str | None = None,
    ) -> ManifestResponse:
        headers = {"If-None-Match": etag} if etag else None
        response = await self._request(
            principal,
            "GET",
            "/nanobot/skills/manifest",
            headers=headers,
        )
        if response.status_code == 304:
            return ManifestResponse(304, None, response.headers.get("etag") or etag)
        payload = self._json_object(response)
        return ManifestResponse(response.status_code, payload, response.headers.get("etag"))

    async def artifact(self, principal: Principal, artifact_path: str) -> ArtifactResponse:
        response = await self._request(
            principal,
            "GET",
            artifact_path,
            headers={"Accept": "application/zip"},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise self._error_from_response(response)
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type and media_type not in {"application/zip", "application/octet-stream"}:
            raise SkillMarketError(
                "ARTIFACT_INVALID",
                "Skill artifact response has an invalid media type",
                http_status=502,
            )
        return ArtifactResponse(response.content, response.headers)

    async def sync_report(
        self,
        principal: Principal,
        payload: Mapping[str, Any],
    ) -> None:
        response = await self._request(
            principal,
            "POST",
            "/nanobot/skills/sync-report",
            json_body=payload,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise self._error_from_response(response)
