from __future__ import annotations

import pytest

from nanobot.webui import nanobot_features_api

VISIBLE_CHANNELS = {
    "dingtalk",
    "email",
    "feishu",
    "qq",
    "websocket",
    "wecom",
    "weixin",
}


def _feature(name: str, *, feature_type: str = "channel", enabled: bool = False) -> dict:
    return {
        "name": name,
        "type": feature_type,
        "enabled": enabled,
    }


def test_nanobot_features_payload_only_exposes_selected_webui_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_channels = VISIBLE_CHANNELS | {"discord", "matrix", "slack", "telegram"}
    payload = {
        "features": [
            *[_feature(name, enabled=True) for name in sorted(all_channels)],
            _feature("bedrock", feature_type="feature", enabled=True),
        ],
        "enabled_count": len(all_channels) + 1,
    }
    monkeypatch.setattr(nanobot_features_api, "optional_features_payload", lambda: payload)

    result = nanobot_features_api.nanobot_features_payload()

    visible_channels = {
        feature["name"]
        for feature in result["features"]
        if feature["type"] == "channel"
    }
    assert visible_channels == VISIBLE_CHANNELS
    assert any(feature["name"] == "bedrock" for feature in result["features"])
    assert result["enabled_count"] == len(VISIBLE_CHANNELS) + 1


def test_nanobot_features_action_keeps_hidden_channels_out_of_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_payload = {
        "features": [
            _feature("feishu", enabled=True),
            _feature("telegram", enabled=True),
        ],
        "enabled_count": 2,
        "last_action": {"ok": True},
    }
    monkeypatch.setattr(
        nanobot_features_api,
        "enable_optional_feature",
        lambda _name, **_kwargs: action_payload,
    )

    result = nanobot_features_api.nanobot_features_action(
        "enable",
        {"name": ["feishu"]},
    )

    assert [feature["name"] for feature in result["features"]] == ["feishu"]
    assert result["enabled_count"] == 1
    assert result["last_action"] == {"ok": True}
