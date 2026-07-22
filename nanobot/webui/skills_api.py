"""Lightweight skill summaries for the WebUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader


def webui_skills_payload(
    workspace_path: Path,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any]:
    """Return agent skills without leaking local filesystem paths."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = sorted(
        loader.list_skills(filter_unavailable=False),
        key=lambda entry: (entry.get("source") != "workspace", entry["name"]),
    )
    return {"skills": [_skill_payload(loader, entry) for entry in entries]}


def webui_skill_detail_payload(
    workspace_path: Path,
    name: str,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return a single skill's safe detail payload."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = loader.list_skills(filter_unavailable=False)
    entry = next((item for item in entries if item["name"] == name), None)
    if entry is None:
        return None
    return {
        **_skill_payload(loader, entry),
        "requirements": loader.get_skill_requirements(name),
        "raw_markdown": loader.load_skill(name) or "",
    }


def managed_skill_summaries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project the active managed inventory into the legacy Skill list shape."""
    raw_items = payload.get("skills", payload.get("items", payload.get("installed", [])))
    if not isinstance(raw_items, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        skill_id = _managed_skill_id(raw)
        if not skill_id:
            continue
        status = str(raw.get("status") or "active").lower()
        result.append({
            "name": skill_id,
            "description": str(
                raw.get("summary") or raw.get("description") or raw.get("displayName") or skill_id
            ),
            "source": "managed",
            "available": status not in {"blocked", "disabled", "failed", "revoked"},
            "unavailable_reason": (
                str(raw.get("error") or status)
                if status in {"blocked", "disabled", "failed", "revoked"}
                else ""
            ),
            "skillId": skill_id,
            "version": raw.get("installedVersion") or raw.get("version"),
            "required": raw.get("required") is True or raw.get("mandatory") is True,
            "updatePolicy": raw.get("updatePolicy"),
            "updateAvailable": raw.get("updateAvailable") is True,
        })
    return result


def managed_skill_detail_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Project safe marketplace detail into the legacy detail response shape."""
    skill_id = _managed_skill_id(payload)
    if not skill_id:
        return None
    status = str(payload.get("status") or "active").lower()
    return {
        "name": skill_id,
        "description": str(
            payload.get("summary")
            or payload.get("description")
            or payload.get("displayName")
            or skill_id
        ),
        "source": "managed",
        "available": status not in {"blocked", "disabled", "failed", "revoked"},
        "unavailable_reason": (
            str(payload.get("error") or status)
            if status in {"blocked", "disabled", "failed", "revoked"}
            else ""
        ),
        "requirements": {"bins": [], "env": [], "missing_bins": [], "missing_env": []},
        "raw_markdown": "",
        "market": payload,
    }


def _skill_payload(loader: SkillsLoader, entry: dict[str, str]) -> dict[str, Any]:
    name = entry["name"]
    metadata = loader.get_skill_metadata(name)
    available, unavailable_reason = loader.get_skill_availability(name)
    return {
        "name": name,
        "description": _description(metadata, name),
        "source": entry.get("source", "unknown"),
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


def _description(metadata: dict[str, Any] | None, fallback: str) -> str:
    if metadata is None:
        return fallback
    value = metadata.get("description")
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _managed_skill_id(payload: dict[str, Any]) -> str:
    for key in ("skillKey", "skillId", "key", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
