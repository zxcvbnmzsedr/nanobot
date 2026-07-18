from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.channels.weixin import WeixinChannel
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.webui.channel_connect import WeixinConnectStore


@pytest.mark.asyncio
async def test_weixin_connect_store_saves_confirmed_qr_login(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "weixin-state"
    config_path = tmp_path / "config.json"
    save_config(
        Config.model_validate({"channels": {"weixin": {"stateDir": str(state_dir)}}}),
        config_path,
    )
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    async def fake_fetch_qr_code(self: WeixinChannel) -> tuple[str, str]:
        return "qr-1", "https://qr.example/1"

    async def fake_api_get_with_base(
        self: WeixinChannel,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, Any],
        auth: bool,
    ) -> dict[str, str]:
        assert base_url == "https://ilinkai.weixin.qq.com"
        assert endpoint == "ilink/bot/get_qrcode_status"
        assert params == {"qrcode": "qr-1"}
        assert auth is False
        return {
            "status": "confirmed",
            "bot_token": "wx-token",
            "baseurl": "https://weixin.example",
            "ilink_user_id": "wx-user",
        }

    monkeypatch.setattr(WeixinChannel, "_fetch_qr_code", fake_fetch_qr_code)
    monkeypatch.setattr(WeixinChannel, "_api_get_with_base", fake_api_get_with_base)

    store = WeixinConnectStore()

    started = await store.start()
    assert started["status"] == "pending"
    assert started["qr_url"] == "https://qr.example/1"

    completed = await store.poll(started["session_id"])
    assert completed["status"] == "succeeded"
    assert completed["account"] == "wx-user"

    saved = json.loads((state_dir / "account.json").read_text())
    assert saved["token"] == "wx-token"
    assert saved["get_updates_buf"] == ""
    assert saved["context_tokens"] == {}
    assert saved["base_url"] == "https://weixin.example"


@pytest.mark.asyncio
async def test_weixin_reconnect_keeps_existing_account_until_scan_succeeds(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "weixin-state"
    state_dir.mkdir()
    existing = {
        "token": "working-token",
        "base_url": "https://working.weixin.example",
        "context_tokens": {"user-1": "context-1"},
    }
    state_file = state_dir / "account.json"
    state_file.write_text(json.dumps(existing), encoding="utf-8")
    config_path = tmp_path / "config.json"
    save_config(
        Config.model_validate({"channels": {"weixin": {"stateDir": str(state_dir)}}}),
        config_path,
    )
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    async def fake_fetch_qr_code(self: WeixinChannel) -> tuple[str, str]:
        return "qr-reconnect", "https://qr.example/reconnect"

    monkeypatch.setattr(WeixinChannel, "_fetch_qr_code", fake_fetch_qr_code)

    store = WeixinConnectStore()
    started = await store.start(force=True)

    assert json.loads(state_file.read_text(encoding="utf-8")) == existing
    cancelled = await store.cancel(started["session_id"])
    assert cancelled["status"] == "cancelled"
    assert json.loads(state_file.read_text(encoding="utf-8")) == existing


@pytest.mark.asyncio
async def test_weixin_create_adds_independent_account_instance(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_state = tmp_path / "weixin-default"
    default_state.mkdir()
    (default_state / "account.json").write_text(
        json.dumps({"token": "default-token"}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    save_config(
        Config.model_validate({
            "channels": {
                "weixin": {
                    "enabled": True,
                    "stateDir": str(default_state),
                },
            },
        }),
        config_path,
    )
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    async def fake_fetch_qr_code(self: WeixinChannel) -> tuple[str, str]:
        return "qr-new", "https://qr.example/new"

    async def fake_poll(self: WeixinChannel, **_kwargs: Any) -> dict[str, str]:
        return {
            "status": "confirmed",
            "bot_token": "second-token",
            "ilink_user_id": "second-account",
        }

    monkeypatch.setattr(WeixinChannel, "_fetch_qr_code", fake_fetch_qr_code)
    monkeypatch.setattr(WeixinChannel, "_api_get_with_base", fake_poll)

    store = WeixinConnectStore()
    started = await store.start(mode="create")
    completed = await store.poll(started["session_id"])

    assert completed["status"] == "succeeded"
    assert completed["instance_id"].startswith("account-")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    instances = data["channels"]["weixin"]["instances"]
    assert {instance["instanceId"] for instance in instances} == {
        "default",
        completed["instance_id"],
    }
    second = next(
        instance for instance in instances if instance["instanceId"] == completed["instance_id"]
    )
    second_state = json.loads((Path(second["stateDir"]) / "account.json").read_text())
    assert second_state["token"] == "second-token"
    assert json.loads((default_state / "account.json").read_text())["token"] == "default-token"
