"""HTTP client for the Java-owned microgrid data boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from nanobot.agent.tools.context import current_request_context
from nanobot.security.network import PinnedDNSAsyncTransport, validate_url_target

_SESSION_PREFIX = "api:microgrid:"


class MicrogridContextError(ValueError):
    """Raised when a request did not originate from the Java agent gateway."""


@dataclass(frozen=True)
class MicrogridRequestContext:
    user_id: str

    @classmethod
    def current(cls) -> "MicrogridRequestContext":
        request = current_request_context()
        session_key = request.session_key if request else None
        if not session_key or not session_key.startswith(_SESSION_PREFIX):
            raise MicrogridContextError("missing trusted microgrid request context")

        parts = session_key.split(":", 4)
        if len(parts) != 5 or not parts[2].isdigit() or parts[3] != "global":
            raise MicrogridContextError("invalid trusted microgrid request context")
        return cls(user_id=parts[2])


class MicrogridClient:
    """Calls only the fixed, read-only endpoints exposed by the Java backend."""

    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.timeout = timeout
        self.transport = transport

    @classmethod
    def from_env(cls) -> "MicrogridClient":
        base_url = os.environ.get("MICROGRID_BACKEND_URL", "").strip()
        service_token = os.environ.get("MICROGRID_AGENT_SERVICE_TOKEN", "").strip()
        if not base_url or not service_token:
            raise MicrogridContextError(
                "MICROGRID_BACKEND_URL and MICROGRID_AGENT_SERVICE_TOKEN are required"
            )
        return cls(base_url, service_token)

    async def list_projects(self, context: MicrogridRequestContext) -> dict[str, Any]:
        return await self._request("GET", "catalog", context)

    async def get(
        self,
        resource: str,
        context: MicrogridRequestContext,
        project_id: str,
    ) -> dict[str, Any]:
        return await self._request("GET", resource, context, project_id=project_id)

    async def post(
        self,
        resource: str,
        context: MicrogridRequestContext,
        payload: dict[str, Any],
        project_id: str,
    ) -> dict[str, Any]:
        return await self._request("POST", resource, context, payload, project_id)

    async def _request(
        self,
        method: str,
        resource: str,
        context: MicrogridRequestContext,
        payload: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if project_id is None:
            url = f"{self.base_url}/internal-api/agent/projects/{resource}"
        else:
            if not isinstance(project_id, str) or not project_id.strip():
                raise MicrogridContextError("project_id must be a non-empty string")
            encoded_project_id = quote(project_id.strip(), safe="")
            url = (
                f"{self.base_url}/internal-api/agent/projects/"
                f"{encoded_project_id}/{resource}"
            )
        ok, error = validate_url_target(url)
        if not ok:
            raise MicrogridContextError(f"microgrid backend URL rejected: {error}")

        transport = self.transport or PinnedDNSAsyncTransport()
        headers = {
            "Accept": "application/json",
            "X-Microgrid-Agent-Token": self.service_token,
            "X-Microgrid-User-Id": context.user_id,
        }
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=self.timeout,
            trust_env=False,
        ) as client:
            response = await client.request(method, url, headers=headers, json=payload)
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("microgrid backend returned a non-object response")
        if payload.get("code") not in (None, 0):
            raise ValueError(str(payload.get("msg") or "microgrid backend rejected the request"))
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise ValueError("microgrid backend returned invalid data")
        return data
