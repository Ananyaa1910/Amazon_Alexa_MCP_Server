"""
Adapter factory — creates Real or Mock adapters based on MOCK_MODE.
"""

from __future__ import annotations

from src.config import Settings

from src.adapters.lights import (
    LightsAdapter,
    MockLightsAdapter,
    RealLightsAdapter,
)
from src.adapters.media import (
    MediaAdapter,
    MockMediaAdapter,
    RealMediaAdapter,
)
from src.adapters.phone_dnd import (
    MacroDroidDndAdapter,
    MockPhoneDndAdapter,
    PhoneDndAdapter,
    PushcutDndAdapter,
)


def create_lights_adapter(settings: Settings) -> LightsAdapter:
    if settings.mock_mode:
        return MockLightsAdapter()
    return RealLightsAdapter(settings)


def create_phone_dnd_adapter(settings: Settings) -> PhoneDndAdapter:
    if settings.mock_mode:
        return MockPhoneDndAdapter()
    if settings.phone_platform == "ios":
        return PushcutDndAdapter(settings)
    return MacroDroidDndAdapter(settings)


def create_media_adapter(settings: Settings) -> MediaAdapter:
    if settings.mock_mode:
        return MockMediaAdapter()
    return RealMediaAdapter(media_url=settings.focus_media_url)
