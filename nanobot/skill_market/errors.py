"""Stable errors exposed by the local Skill marketplace service."""

from __future__ import annotations

from typing import Any


class SkillMarketError(RuntimeError):
    """A sanitized marketplace failure suitable for the WebSocket boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class SkillArtifactError(SkillMarketError):
    """A downloaded release failed cryptographic or package validation."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, http_status=422, retryable=False, details=details)
