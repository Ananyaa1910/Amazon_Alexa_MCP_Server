"""
Scene definitions and scene engine.

Scenes are DATA, not code. Each scene maps adapter names to their
desired state. The engine applies a scene by calling all adapters
CONCURRENTLY — one failing adapter must NOT block the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.adapters.base import AdapterResult
from src.adapters.lights import LightsAdapter
from src.adapters.media import MediaAdapter
from src.adapters.phone_dnd import PhoneDndAdapter
from src.events import EventBus

logger = logging.getLogger("ambient_focus_agent.scenes")

# ── Scene definitions (data, not code) ─────────────────────────────────────

SCENES: dict[str, dict[str, dict[str, Any]]] = {
    "focus": {
        "lights": {"color": "blue", "brightness": 80},
        "dnd": {"enabled": True},
        "media": {"playing": True},
    },
    "break": {
        "lights": {"color": "green", "brightness": 60},
        "dnd": {"enabled": False},
        "media": {"playing": False},
    },
    "idle": {
        "lights": {"color": "white", "brightness": 100},
        "dnd": {"enabled": False},
        "media": {"playing": False},
    },
}


class SceneEngine:
    """
    Applies scenes by calling adapters concurrently.

    One adapter failing does NOT stop the others or the session.
    Results report per-adapter {ok, error?}.
    """

    def __init__(
        self,
        lights: LightsAdapter,
        phone_dnd: PhoneDndAdapter,
        media: MediaAdapter,
        event_bus: EventBus,
        media_url: str = "",
    ) -> None:
        self.lights = lights
        self.phone_dnd = phone_dnd
        self.media = media
        self.event_bus = event_bus
        self.media_url = media_url

    async def apply_scene(
        self,
        scene_name: str,
        *,
        overrides: dict[str, bool] | None = None,
        restore_state: dict | None = None,
    ) -> dict[str, AdapterResult]:
        """
        Apply a named scene (or a custom restore state).

        ``overrides`` can disable individual adapters for this application,
        e.g. ``{"dnd": False}`` means skip the DND adapter entirely.

        ``restore_state`` is used for the 'restore' pseudo-scene — it
        contains the pre-focus light state to restore.
        """
        if restore_state:
            scene_data = {
                "lights": restore_state,
                "dnd": {"enabled": False},
                "media": {"playing": False},
            }
        else:
            scene_data = SCENES.get(scene_name, SCENES["idle"]).copy()

        # Apply overrides: if override says lights=False, remove lights
        if overrides:
            if overrides.get("lights") is False and "lights" in scene_data:
                del scene_data["lights"]
            if overrides.get("dnd") is False and "dnd" in scene_data:
                del scene_data["dnd"]
            if overrides.get("music") is False and "media" in scene_data:
                del scene_data["media"]

        # Build adapter coroutines
        tasks: dict[str, Any] = {}
        if "lights" in scene_data:
            ld = scene_data["lights"]
            tasks["lights"] = self.lights.set_lights(
                color=ld.get("color", "white"),
                brightness=ld.get("brightness", 100),
            )
        if "dnd" in scene_data:
            tasks["dnd"] = self.phone_dnd.set_dnd(
                enabled=scene_data["dnd"].get("enabled", False),
            )
        if "media" in scene_data:
            tasks["media"] = self.media.control(
                playing=scene_data["media"].get("playing", False),
                url=self.media_url,
            )

        # Run all concurrently — exceptions are caught per-adapter
        keys = list(tasks.keys())
        raw_results = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )

        results: dict[str, AdapterResult] = {}
        for key, result in zip(keys, raw_results):
            if isinstance(result, BaseException):
                error_msg = f"{type(result).__name__}: {result}"
                logger.error("Adapter '%s' raised: %s", key, error_msg)
                results[key] = AdapterResult(
                    adapter=key, action=scene_name, ok=False, error=error_msg,
                )
                await self.event_bus.emit("adapter_failed", {
                    "adapter": key,
                    "scene": scene_name,
                    "error": error_msg,
                })
            else:
                results[key] = result
                if not result.ok:
                    await self.event_bus.emit("adapter_failed", {
                        "adapter": key,
                        "scene": scene_name,
                        "error": result.error,
                    })
                # Emit media event so the web UI can react
                if key == "media" and result.ok:
                    await self.event_bus.emit("media", result.details)

        await self.event_bus.emit("scene_applied", {
            "scene": scene_name,
            "results": {k: v.to_dict() for k, v in results.items()},
        })

        return results

    def get_scene_state(self, scene_name: str) -> dict:
        """Return the data definition for a named scene."""
        return SCENES.get(scene_name, SCENES["idle"])
