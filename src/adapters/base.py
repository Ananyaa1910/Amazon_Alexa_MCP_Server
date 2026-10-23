"""
Adapter base classes and result type.

Every adapter (lights, phone DND, media) implements a common result type.
Real adapters make HTTP calls; Mock adapters log and return success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("ambient_focus_agent.adapters")


@dataclass
class AdapterResult:
    """Outcome of a single adapter call."""

    adapter: str          # "lights" | "phone_dnd" | "media"
    action: str           # human-readable description
    ok: bool = True
    error: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {
            "adapter": self.adapter,
            "action": self.action,
            "ok": self.ok,
        }
        if self.error:
            d["error"] = self.error
        if self.details:
            d["details"] = self.details
        return d
