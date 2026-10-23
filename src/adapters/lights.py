"""
Lights adapter — IFTTT Maker Webhooks.

Real: POST https://maker.ifttt.com/trigger/{event}/with/key/{KEY}
Mock: logs the intended action, returns success.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from src.adapters.base import AdapterResult
from src.config import Settings, redact_url

logger = logging.getLogger("ambient_focus_agent.adapters.lights")

# Maps logical colour names to scene-specific IFTTT events
_COLOR_TO_EVENT_FIELD = {
    "blue": "focus",
    "green": "break",
    "warm_white": "restore",
    "white": "restore",
}


class LightsAdapter(ABC):
    """Interface for smart-light control."""

    @abstractmethod
    async def set_lights(self, color: str, brightness: int) -> AdapterResult:
        ...

    @abstractmethod
    def get_last_state(self) -> dict:
        """Return the last commanded {color, brightness}."""
        ...


class RealLightsAdapter(LightsAdapter):
    """Calls IFTTT Maker Webhooks to control lights."""

    def __init__(self, settings: Settings) -> None:
        self._key = settings.ifttt_webhook_key
        self._events = {
            "focus": settings.ifttt_event_focus,
            "break": settings.ifttt_event_break,
            "restore": settings.ifttt_event_restore,
        }
        self._timeout = settings.adapter_timeout_seconds
        self._last: dict = {"color": "white", "brightness": 100}

    def _url(self, event_name: str) -> str:
        return f"https://maker.ifttt.com/trigger/{event_name}/with/key/{self._key}"

    async def set_lights(self, color: str, brightness: int) -> AdapterResult:
        scene_key = _COLOR_TO_EVENT_FIELD.get(color, "restore")
        event_name = self._events.get(scene_key, self._events["restore"])
        url = self._url(event_name)
        payload = {"value1": color, "value2": str(brightness)}

        # 1 retry on 5xx / timeout
        last_error: str = ""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=payload)
                if resp.status_code < 500:
                    self._last = {"color": color, "brightness": brightness}
                    ok = resp.status_code < 400
                    return AdapterResult(
                        adapter="lights",
                        action=f"set_lights({color}, {brightness})",
                        ok=ok,
                        error=None if ok else f"HTTP {resp.status_code}",
                        details={"status_code": resp.status_code, "attempt": attempt + 1},
                    )
                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "Lights adapter 5xx (attempt %d): %s → %d",
                    attempt + 1,
                    redact_url(url),
                    resp.status_code,
                )
            except httpx.TimeoutException:
                last_error = f"Timeout after {self._timeout}s"
                logger.warning(
                    "Lights adapter timeout (attempt %d): %s",
                    attempt + 1,
                    redact_url(url),
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                logger.warning(
                    "Lights adapter HTTP error (attempt %d): %s",
                    attempt + 1,
                    type(exc).__name__,
                )
                break  # don't retry on connection errors

        return AdapterResult(
            adapter="lights",
            action=f"set_lights({color}, {brightness})",
            ok=False,
            error=last_error,
        )

    def get_last_state(self) -> dict:
        return dict(self._last)


class MockLightsAdapter(LightsAdapter):
    """Logs what it WOULD do and returns success. Zero HTTP calls."""

    def __init__(self) -> None:
        self._last: dict = {"color": "white", "brightness": 100}
        self.call_log: list[dict] = []

    async def set_lights(self, color: str, brightness: int) -> AdapterResult:
        entry = {"color": color, "brightness": brightness}
        self.call_log.append(entry)
        logger.info("[MOCK] Lights → color=%s brightness=%d", color, brightness)
        self._last = entry
        return AdapterResult(
            adapter="lights",
            action=f"set_lights({color}, {brightness})",
            ok=True,
            details={"mock": True},
        )

    def get_last_state(self) -> dict:
        return dict(self._last)
