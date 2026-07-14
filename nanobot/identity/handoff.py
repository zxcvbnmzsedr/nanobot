"""One-time, short-lived browser handoff codes."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from nanobot.identity.principal import Principal


@dataclass
class HandoffStore:
    max_codes: int = 10_000
    _codes: dict[str, tuple[float, Principal]] = field(default_factory=dict)

    def issue(self, principal: Principal, ttl_s: int | float) -> str:
        self._purge()
        if len(self._codes) >= self.max_codes:
            raise OverflowError("too many outstanding handoff codes")
        code = f"nbho_{secrets.token_urlsafe(32)}"
        self._codes[code] = (time.monotonic() + float(ttl_s), principal)
        return code

    def consume(self, code: str | None) -> Principal | None:
        if not code:
            return None
        self._purge()
        item = self._codes.pop(code, None)
        if item is None:
            return None
        expiry, principal = item
        return principal if time.monotonic() <= expiry else None

    def clear(self) -> None:
        self._codes.clear()

    def _purge(self) -> None:
        now = time.monotonic()
        for code, (expiry, _) in list(self._codes.items()):
            if now > expiry:
                self._codes.pop(code, None)
