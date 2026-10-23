"""
Phone DND adapter — MacroDroid (Android) or Pushcut (iOS).

Real: HTTP call to the configured webhook URL / notification endpoint.
Mock: logs the intended action, returns success.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from src.adapters.base import AdapterResult
from src.config import Settings, redact_url

logger = logging.getLogger("ambient_focus_agent.adapters.phone_dnd")


class PhoneDndAdapter(ABC):
    """Interface for phone Do Not Disturb control."""

    @abstractmethod
    async def set_dnd(self, enabled: bool) -> AdapterResult:
        ...


class MacroDroidDndAdapter(PhoneDndAdapter):
    """Android DND via MacroDroid webhook URLs."""

    def __init__(self, settings: Settings) -> None:
        self._url_on = settings.macrodroid_webhook_url_dnd_on
        self._url_off = settings.macrodroid_webhook_url_dnd_off
        self._timeout = settings.adapter_timeout_seconds

    async def set_dnd(self, enabled: bool) -> AdapterResult:
        url = self._url_on if enabled else self._url_off
        action = f"dnd_{'on' if enabled else 'off'}"

        last_error: str = ""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(url)
                if resp.status_code < 500:
                    ok = resp.status_code < 400
                    return AdapterResult(
                        adapter="phone_dnd",
                        action=action,
                        ok=ok,
                        error=None if ok else f"HTTP {resp.status_code}",
                        details={
                            "platform": "android",
                            "status_code": resp.status_code,
                            "attempt": attempt + 1,
                        },
                    )
                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "MacroDroid 5xx (attempt %d): %s → %d",
                    attempt + 1,
                    redact_url(url),
                    resp.status_code,
                )
            except httpx.TimeoutException:
                last_error = f"Timeout after {self._timeout}s"
                logger.warning(
                    "MacroDroid timeout (attempt %d): %s",
                    attempt + 1,
                    redact_url(url),
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                logger.warning(
                    "MacroDroid HTTP error (attempt %d): %s",
                    attempt + 1,
                    type(exc).__name__,
                )
                break
        return AdapterResult(
            adapter="phone_dnd", action=action, ok=False, error=last_error,
            details={"platform": "android"},
        )


class PushcutDndAdapter(PhoneDndAdapter):
    """iOS DND via Pushcut notification endpoint."""

    _BASE = "https://api.pushcut.io"

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.pushcut_api_key
        self._notif_on = settings.pushcut_notification_dnd_on
        self._notif_off = settings.pushcut_notification_dnd_off
        self._timeout = settings.adapter_timeout_seconds

    async def set_dnd(self, enabled: bool) -> AdapterResult:
        notif = self._notif_on if enabled else self._notif_off
        action = f"dnd_{'on' if enabled else 'off'}"
        url = f"{self._BASE}/v1/notifications/{notif}"
        headers = {"API-Key": self._api_key}

        last_error: str = ""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, headers=headers, json={})
                if resp.status_code < 500:
                    ok = resp.status_code < 400
                    return AdapterResult(
                        adapter="phone_dnd",
                        action=action,
                        ok=ok,
                        error=None if ok else f"HTTP {resp.status_code}",
                        details={
                            "platform": "ios",
                            "status_code": resp.status_code,
                            "attempt": attempt + 1,
                        },
                    )
                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "Pushcut 5xx (attempt %d): %s → %d",
                    attempt + 1,
                    redact_url(url),
                    resp.status_code,
                )
            except httpx.TimeoutException:
                last_error = f"Timeout after {self._timeout}s"
                logger.warning("Pushcut timeout (attempt %d)", attempt + 1)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                logger.warning("Pushcut HTTP error (attempt %d): %s", attempt + 1, type(exc).__name__)
                break
        return AdapterResult(
            adapter="phone_dnd", action=action, ok=False, error=last_error,
            details={"platform": "ios"},
        )


class MockPhoneDndAdapter(PhoneDndAdapter):
    """Logs what it WOULD do and returns success. Zero HTTP calls."""

    def __init__(self) -> None:
        self.call_log: list[dict] = []

    async def set_dnd(self, enabled: bool) -> AdapterResult:
        entry = {"enabled": enabled}
        self.call_log.append(entry)
        logger.info("[MOCK] Phone DND → enabled=%s", enabled)
        return AdapterResult(
            adapter="phone_dnd",
            action=f"dnd_{'on' if enabled else 'off'}",
            ok=True,
            details={"mock": True},
        )
