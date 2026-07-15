import hashlib
import time
from typing import Any

import httpx
import pytest

from nanobot.identity.kangaroo import KangarooIdentityError, KangarooIdentityVerifier


class _FakeClient:
    responses: list[httpx.Response]
    requests: list[tuple[str, str, dict[str, Any]]]

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_verify_uses_server_user_and_org(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.responses = [httpx.Response(200, json={
        "data": {"id": 101, "orgId": 9001, "name": "Alice", "orgName": "Org"}
    })]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    verifier = KangarooIdentityVerifier(
        api_base="https://accounts.example.com/",
        user_info_path="api/auth/userInfo",
        timeout_s=5,
    )

    principal = await verifier.verify("access-token")

    assert principal.user_id == "101"
    assert principal.org_id == "9001"


@pytest.mark.asyncio
async def test_login_uses_kangaroo_contract_then_verifies_user_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.responses = [
        httpx.Response(200, json={"data": {"value": "access-token"}}),
        httpx.Response(200, json={"data": {"id": 101, "orgId": 9001}}),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    verifier = KangarooIdentityVerifier(
        api_base="https://accounts.example.com/",
        user_info_path="api/auth/userInfo",
        login_path="api/auth/oauth/login",
        timeout_s=5,
    )

    principal = await verifier.login("13800138000", "secret-password")

    assert principal.user_id == "101"
    method, url, kwargs = _FakeClient.requests[0]
    assert (method, url) == (
        "POST",
        "https://accounts.example.com/api/auth/oauth/login",
    )
    assert kwargs["headers"]["Authorization"].startswith("Basic ")
    expected_password = hashlib.md5(
        "secret-password!eSa36J5s!gdYQPF".encode(),
        usedforsecurity=False,
    ).hexdigest()
    assert kwargs["json"] == {
        "phone": "13800138000",
        "password": expected_password,
        "type": "kangaroo-intelligent-web",
        "captchaKey": "",
        "captchaValue": "",
        "grant_type": "password",
        "scope": "all",
    }
    assert _FakeClient.requests[1][2]["headers"]["Authorization"] == "Bearer access-token"


@pytest.mark.asyncio
async def test_login_captures_refresh_token_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_700_000_000.0
    _FakeClient.responses = [
        httpx.Response(200, json={
            "data": {
                "value": "access-token",
                "expiration": (now + 3600) * 1000,
                "refreshToken": {
                    "value": "refresh-token",
                    "expiration": (now + 86_400) * 1000,
                },
            }
        }),
        httpx.Response(200, json={"data": {"id": 101, "orgId": 9001}}),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    verifier = KangarooIdentityVerifier(
        api_base="https://accounts.example.com/",
        user_info_path="api/auth/userInfo",
        timeout_s=5,
    )

    identity = await verifier.login_with_access_token("13800138000", "secret-password")

    assert identity.access_token == "access-token"
    assert identity.refresh_token == "refresh-token"
    assert identity.expires_at == now + 3600
    assert identity.refresh_expires_at == now + 86_400


@pytest.mark.asyncio
async def test_refresh_rotates_tokens_using_kangaroo_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.responses = [
        httpx.Response(200, json={
            "data": {
                "access_token": "next-access",
                "refresh_token": "next-refresh",
                "expires_in": 3600,
            }
        })
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    verifier = KangarooIdentityVerifier(
        api_base="https://accounts.example.com/",
        user_info_path="api/auth/userInfo",
        refresh_path="api/auth/oauth/token",
        timeout_s=5,
    )

    before = time.time()
    bundle = await verifier.refresh("old-refresh")
    after = time.time()

    assert bundle.access_token == "next-access"
    assert bundle.refresh_token == "next-refresh"
    assert bundle.expires_at is not None
    assert before + 3600 <= bundle.expires_at <= after + 3600
    method, url, kwargs = _FakeClient.requests[0]
    assert (method, url) == (
        "POST",
        "https://accounts.example.com/api/auth/oauth/token",
    )
    assert kwargs["headers"]["Authorization"].startswith("Basic ")
    assert kwargs["json"] == {
        "refresh_token": "old-refresh",
        "grant_type": "refresh_token",
    }


@pytest.mark.asyncio
async def test_verify_rejects_disallowed_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.responses = [
        httpx.Response(200, json={"data": {"id": 101, "orgId": 9001}})
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    verifier = KangarooIdentityVerifier(
        api_base="https://accounts.example.com/",
        user_info_path="api/auth/userInfo",
        timeout_s=5,
        allowed_user_ids={"202"},
    )

    with pytest.raises(KangarooIdentityError, match="not allowed"):
        await verifier.verify("access-token")
