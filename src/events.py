"""
Server-Sent Events (SSE) bus.

Broadcasts session events to all connected SSE clients (the web UI dashboard).
Event types: session_started, tick, phase_changed, scene_applied,
             session_ended, adapter_failed, media.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("ambient_focus_agent.events")


@dataclass
class SSEEvent:
    """A single event to push to SSE subscribers."""

    event: str                   # event type name
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def format_sse(self) -> str:
        payload = {**self.data, "_ts": self.timestamp}
        return f"event: {self.event}\ndata: {json.dumps(payload)}\n\n"


class EventBus:
    """
    Async fan-out event bus.

    - ``emit(event, data)`` pushes to all subscribers.
    - ``subscribe()`` returns an async generator of SSEEvent objects.
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[SSEEvent | None]] = []
        self._lock = asyncio.Lock()

    async def emit(self, event: str, data: dict | None = None) -> None:
        """Broadcast an event to every connected subscriber."""
        sse = SSEEvent(event=event, data=data or {})
        logger.debug("Event emitted: %s", event)
        async with self._lock:
            for q in self._queues:
                try:
                    q.put_nowait(sse)
                except asyncio.QueueFull:
                    logger.warning("SSE subscriber queue full, dropping event %s", event)

    async def subscribe(self):
        """
        Async generator that yields SSEEvent objects until the bus
        sends a ``None`` sentinel (shutdown) or the client disconnects.
        """
        q: asyncio.Queue[SSEEvent | None] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._queues.append(q)
        try:
            while True:
                event = await q.get()
                if event is None:
                    break
                yield event
        finally:
            async with self._lock:
                self._queues.remove(q)

    async def close(self) -> None:
        """Shut down all subscribers by sending the None sentinel."""
        async with self._lock:
            for q in self._queues:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)
