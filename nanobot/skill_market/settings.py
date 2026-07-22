"""Runtime settings for the local Skill marketplace client."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from nanobot import __version__


@dataclass(frozen=True, slots=True)
class SkillPackageLimits:
    max_archive_bytes: int = 1 * 1024 * 1024
    max_unpacked_bytes: int = 4 * 1024 * 1024
    max_file_bytes: int = 512 * 1024
    max_files: int = 128
    max_compression_ratio: float = 20.0

    def __post_init__(self) -> None:
        values = (
            self.max_archive_bytes,
            self.max_unpacked_bytes,
            self.max_file_bytes,
            self.max_files,
        )
        if any(value <= 0 for value in values) or self.max_compression_ratio <= 0:
            raise ValueError("Skill package limits must be positive")


@dataclass(frozen=True, slots=True)
class SkillMarketSettings:
    """Explicit control-plane and storage settings, independent of channel config."""

    runtime_root: Path
    base_url: str = ""
    public_keys: Mapping[str, str | bytes] = field(default_factory=dict)
    enabled: bool = False
    request_timeout_s: float = 30.0
    default_poll_s: int = 300
    min_poll_s: int = 30
    max_poll_s: int = 900
    max_stale_s: int = 86_400
    require_signatures: bool = True
    runtime_version: str = __version__
    limits: SkillPackageLimits = field(default_factory=SkillPackageLimits)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if self.enabled and (parsed.scheme not in {"http", "https"} or not parsed.netloc):
            raise ValueError("enabled Skill marketplace requires an absolute HTTP(S) base_url")
        if self.base_url and (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an absolute HTTP(S) origin")
        if self.enabled and not self.public_keys:
            raise ValueError("enabled Skill marketplace requires at least one public key")
        if self.request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        if not (1 <= self.min_poll_s <= self.default_poll_s <= self.max_poll_s):
            raise ValueError("poll interval must satisfy min <= default <= max")
        if self.max_stale_s <= 0:
            raise ValueError("max_stale_s must be positive")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(
            self,
            "runtime_root",
            self.runtime_root.expanduser().resolve(strict=False),
        )
        object.__setattr__(self, "public_keys", MappingProxyType(dict(self.public_keys)))
