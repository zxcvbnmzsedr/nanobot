"""Persisted provenance markers for content returned by ``read_skill``."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from nanobot.skill_market.errors import SkillMarketError
from nanobot.skill_market.store import ManagedSkillStore

_MARKER = re.compile(r"^<!-- nanobot-skill-provenance:(\{[^\n]+\}) -->\n")


def add_provenance(content: str, payload: Mapping[str, Any]) -> str:
    marker = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"<!-- nanobot-skill-provenance:{marker} -->\n{content}"


def parse_provenance(content: str) -> dict[str, Any] | None:
    match = _MARKER.match(content)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def provenance_only(content: str) -> str | None:
    """Return a replay-safe stub when content starts with a valid provenance marker."""
    match = _MARKER.match(content)
    if match is None or parse_provenance(content) is None:
        return None
    return (
        content[: match.end()]
        + "Managed Skill body omitted from persistent history. Call read_skill again if needed."
    )


def filter_revoked_skill_history(
    history: Sequence[Mapping[str, Any]],
    store: ManagedSkillStore | None,
) -> list[dict[str, Any]]:
    """Replace replayed content from explicitly revoked release digests."""
    revocation_state_invalid = False
    try:
        revoked = store.revoked_digests() if store is not None else set()
    except SkillMarketError:
        revoked = set()
        revocation_state_invalid = True
    output: list[dict[str, Any]] = []
    for original in history:
        message = dict(original)
        content = message.get("content")
        is_skill_result = (
            message.get("role") == "tool" and message.get("name") == "read_skill"
        )
        if revocation_state_invalid and is_skill_result:
            message["content"] = (
                "This Skill content was removed from replay because local revocation state "
                "could not be verified."
            )
        elif (
            revoked
            and is_skill_result
            and isinstance(content, str)
        ):
            provenance = parse_provenance(content)
            digest = provenance.get("sha256") if provenance else None
            if isinstance(digest, str) and digest in revoked:
                message["content"] = (
                    "This Skill content was removed from replay because its signed release "
                    "has been revoked."
                )
        output.append(message)
    return output
