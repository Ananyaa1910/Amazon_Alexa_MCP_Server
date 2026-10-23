"""
Media adapter.

Does NOT call a third party — it emits a ``media`` event with
FOCUS_MEDIA_URL + play/stop so the web UI handles playback via
HTML5 / Web Audio.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from src.adapters.base import AdapterResult

logger = logging.getLogger("ambient_focus_agent.adapters.media")


class MediaAdapter(ABC):
    """Interface for focus music / ambient audio control."""

    @abstractmethod
    async def control(self, playing: bool, url: str = "") -> AdapterResult:
        ...


class RealMediaAdapter(MediaAdapter):
    """
    'Real' media adapter — no HTTP call. Returns the media_url and
    play/stop state for the web UI to act on. The event bus pushes
    this to SSE clients.
    """

    def __init__(self, media_url: str = "") -> None:
        self._media_url = media_url

    async def control(self, playing: bool, url: str = "") -> AdapterResult:
        effective_url = url or self._media_url
        action = "play" if playing else "stop"
        logger.info("Media → %s (url=%s)", action, effective_url or "(web-audio)")
        return AdapterResult(
            adapter="media",
            action=action,
            ok=True,
            details={"playing": playing, "url": effective_url},
        )


class MockMediaAdapter(MediaAdapter):
    """Logs what it WOULD do and returns success."""

    def __init__(self) -> None:
        self.call_log: list[dict] = []

    async def control(self, playing: bool, url: str = "") -> AdapterResult:
        action = "play" if playing else "stop"
        entry = {"playing": playing, "url": url}
        self.call_log.append(entry)
        logger.info("[MOCK] Media → %s", action)
        return AdapterResult(
            adapter="media",
            action=action,
            ok=True,
            details={"mock": True, "playing": playing, "url": url},
        )
