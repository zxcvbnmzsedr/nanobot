"""Nanobot optional feature helpers for WebUI Settings."""
from __future__ import annotations

from typing import Any

from nanobot.channels._feishu_instances import DEFAULT_INSTANCE_ID
from nanobot.optional_features import (
    OptionalFeatureError,
    disable_optional_feature,
    enable_optional_feature,
    optional_features_payload,
)
from nanobot.webui.http_utils import query_first

QueryParams = dict[str, list[str]]

WEBUI_CHANNEL_NAMES = frozenset({
    "dingtalk",
    "email",
    "feishu",
    "qq",
    "websocket",
    "wecom",
    "weixin",
})


def _webui_features_only(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    features = [
        feature
        for feature in payload.get("features", [])
        if feature.get("type") != "channel" or feature.get("name") in WEBUI_CHANNEL_NAMES
    ]
    result["features"] = features
    result["enabled_count"] = sum(1 for feature in features if feature.get("enabled"))
    return result


def nanobot_features_payload() -> dict[str, Any]:
    return _webui_features_only(optional_features_payload())


def nanobot_features_action(
    action: str,
    query: QueryParams,
    *,
    allow_install: bool = True,
) -> dict[str, Any]:
    name = (query_first(query, "name") or "").strip()
    instance_id = (query_first(query, "instance_id") or DEFAULT_INSTANCE_ID).strip()
    if not name:
        raise OptionalFeatureError("missing feature name")
    if action == "enable":
        payload = enable_optional_feature(
            name,
            allow_install=allow_install,
            instance_id=instance_id,
        )
        return _webui_features_only(payload)
    if action == "disable":
        if name == "websocket":
            raise OptionalFeatureError(
                "The WebUI websocket channel cannot be disabled from WebUI. "
                "Use `nanobot plugins disable websocket` from a terminal if you need to disable it.",
                status=400,
            )
        return _webui_features_only(disable_optional_feature(name, instance_id=instance_id))
    raise OptionalFeatureError(f"unknown feature action '{action}'", status=404)
