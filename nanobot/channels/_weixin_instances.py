"""Configuration helpers for multiple personal WeChat accounts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.channels._feishu_instances import (
    DEFAULT_INSTANCE_ID,
    ChannelInstanceSpec,
    runtime_channel_name,
    validate_instance_id,
)
from nanobot.config.loader import merge_missing_defaults


def _as_dict(section: Any) -> dict[str, Any]:
    if hasattr(section, "model_dump"):
        section = section.model_dump(mode="json", by_alias=True)
    return dict(section) if isinstance(section, dict) else {}


def _normalize_instance(
    raw: dict[str, Any],
    defaults: dict[str, Any],
    *,
    inherited: dict[str, Any] | None = None,
    fallback_id: str = DEFAULT_INSTANCE_ID,
) -> dict[str, Any]:
    config = merge_missing_defaults(inherited or {}, defaults)
    config = merge_missing_defaults(raw, config)
    raw_id = raw.get("id") or raw.get("instanceId") or raw.get("instance_id") or fallback_id
    instance_id = validate_instance_id(str(raw_id))
    config["id"] = instance_id
    config["instanceId"] = instance_id
    config.setdefault(
        "name",
        "WeChat account" if instance_id == DEFAULT_INSTANCE_ID else f"WeChat {instance_id}",
    )
    return config


def weixin_instance_specs(
    section: Any,
    defaults: dict[str, Any],
    *,
    enabled_only: bool = False,
) -> list[ChannelInstanceSpec]:
    """Expand legacy flat or canonical ``instances`` config into runtime specs."""
    section_dict = _as_dict(section)
    instances = section_dict.get("instances")
    if isinstance(instances, list):
        inherited = {key: value for key, value in section_dict.items() if key != "instances"}
        raw_specs = [item for item in instances if isinstance(item, dict)]
    else:
        inherited = None
        raw_specs = [section_dict] if section_dict else [{"id": DEFAULT_INSTANCE_ID}]

    specs: list[ChannelInstanceSpec] = []
    for index, raw in enumerate(raw_specs):
        fallback_id = DEFAULT_INSTANCE_ID if index == 0 else f"account-{index + 1}"
        try:
            config = _normalize_instance(
                raw,
                defaults,
                inherited=inherited,
                fallback_id=fallback_id,
            )
        except ValueError as exc:
            logger.warning("Skipping invalid WeChat instance config: {}", exc)
            continue
        if enabled_only and not bool(config.get("enabled", False)):
            continue
        instance_id = str(config["instanceId"])
        specs.append(
            ChannelInstanceSpec(
                base_name="weixin",
                instance_id=instance_id,
                runtime_name=runtime_channel_name("weixin", instance_id),
                config=config,
            )
        )
    return specs


def canonical_weixin_section(section: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    """Return the section in the canonical multi-instance shape."""
    return {"instances": [dict(spec.config) for spec in weixin_instance_specs(section, defaults)]}


def upsert_weixin_instance(
    section: Any,
    defaults: dict[str, Any],
    instance_id: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Create or update one WeChat instance without exposing its token in config."""
    instance_id = validate_instance_id(instance_id)
    canonical = canonical_weixin_section(section, defaults)
    instances = canonical["instances"]
    for instance in instances:
        if instance.get("id") == instance_id or instance.get("instanceId") == instance_id:
            instance.update(values)
            instance["id"] = instance_id
            instance["instanceId"] = instance_id
            return canonical
    instances.append(
        _normalize_instance(
            {**values, "id": instance_id},
            defaults,
            fallback_id=instance_id,
        )
    )
    return canonical


def set_weixin_instance_enabled(
    section: Any,
    defaults: dict[str, Any],
    instance_id: str,
    enabled: bool,
) -> dict[str, Any]:
    return upsert_weixin_instance(section, defaults, instance_id, {"enabled": enabled})


def instance_state_file(config: dict[str, Any], default_root: Path) -> Path:
    configured = str(config.get("stateDir") or config.get("state_dir") or "").strip()
    state_dir = Path(configured).expanduser() if configured else default_root
    return state_dir / "account.json"


def instance_has_login_state(config: dict[str, Any], default_root: Path) -> bool:
    import json

    try:
        payload = json.loads(instance_state_file(config, default_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(str(payload.get("token") or "").strip())
