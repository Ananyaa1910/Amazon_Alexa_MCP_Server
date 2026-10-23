"""
Typed configuration module.

Loads environment variables from .env via python-dotenv, validates them,
and fails fast with a clear message when a REAL-mode variable is missing.
Secret values are NEVER logged.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("ambient_focus_agent.config")

# ── Fields that contain secrets — never log their values ───────────────────
_SECRET_FIELDS = frozenset({
    "mcp_auth_token",
    "ifttt_webhook_key",
    "pushcut_api_key",
    "macrodroid_webhook_url_dnd_on",
    "macrodroid_webhook_url_dnd_off",
})


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def _parse_int(value: str, name: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        raise ValueError(f"Environment variable {name} must be an integer, got '{value}'")


def _parse_origins(value: str) -> list[str]:
    return [o.strip() for o in value.split(",") if o.strip()]


def redact_secret(value: str) -> str:
    """Return a redacted representation for logging."""
    if not value:
        return "(empty)"
    return value[:2] + "***" + value[-1] if len(value) > 3 else "***"


def redact_url(url: str) -> str:
    """Replace API keys and tokens embedded in URLs with ***."""
    return re.sub(
        r"(key/|token/|api_key=|key=)([^/&?\s]+)",
        r"\1***",
        url,
    )


@dataclass(frozen=True)
class Settings:
    """Immutable application settings — validated once at startup."""

    # Mode
    mock_mode: bool = True

    # Server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_path: str = "/mcp"
    allowed_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    mcp_auth_token: str = "change-me"

    # IFTTT
    ifttt_webhook_key: str = ""
    ifttt_event_focus: str = "focus_mode"
    ifttt_event_break: str = "break_mode"
    ifttt_event_restore: str = "restore_lights"

    # Phone
    phone_platform: str = "android"
    macrodroid_webhook_url_dnd_on: str = ""
    macrodroid_webhook_url_dnd_off: str = ""
    pushcut_api_key: str = ""
    pushcut_notification_dnd_on: str = ""
    pushcut_notification_dnd_off: str = ""

    # Media
    focus_media_url: str = ""

    # Defaults
    default_focus_minutes: int = 25
    default_break_minutes: int = 5
    confirmation_ttl_seconds: int = 60
    adapter_timeout_seconds: int = 5

    # ── Validation ─────────────────────────────────────────────────────
    def validate(self) -> None:
        """
        Fail fast with clear messages if required variables are missing.
        In MOCK_MODE no third-party keys are required.
        """
        errors: list[str] = []

        if self.phone_platform not in ("android", "ios"):
            errors.append(
                f"PHONE_PLATFORM must be 'android' or 'ios', got '{self.phone_platform}'"
            )

        if self.default_focus_minutes < 1 or self.default_focus_minutes > 120:
            errors.append("DEFAULT_FOCUS_MINUTES must be between 1 and 120")

        if self.default_break_minutes < 1 or self.default_break_minutes > 60:
            errors.append("DEFAULT_BREAK_MINUTES must be between 1 and 60")

        if self.adapter_timeout_seconds < 1:
            errors.append("ADAPTER_TIMEOUT_SECONDS must be >= 1")

        if self.confirmation_ttl_seconds < 5:
            errors.append("CONFIRMATION_TTL_SECONDS must be >= 5")

        # Real-mode requirements
        if not self.mock_mode:
            if not self.ifttt_webhook_key:
                errors.append("IFTTT_WEBHOOK_KEY is required when MOCK_MODE=false")

            if self.phone_platform == "android":
                if not self.macrodroid_webhook_url_dnd_on:
                    errors.append(
                        "MACRODROID_WEBHOOK_URL_DND_ON is required for android platform"
                    )
                if not self.macrodroid_webhook_url_dnd_off:
                    errors.append(
                        "MACRODROID_WEBHOOK_URL_DND_OFF is required for android platform"
                    )
            elif self.phone_platform == "ios":
                if not self.pushcut_api_key:
                    errors.append("PUSHCUT_API_KEY is required for ios platform")
                if not self.pushcut_notification_dnd_on:
                    errors.append(
                        "PUSHCUT_NOTIFICATION_DND_ON is required for ios platform"
                    )
                if not self.pushcut_notification_dnd_off:
                    errors.append(
                        "PUSHCUT_NOTIFICATION_DND_OFF is required for ios platform"
                    )

        if errors:
            msg = "Configuration errors:\n" + "\n".join(f"  ✗ {e}" for e in errors)
            raise ValueError(msg)

    # ── Safe representation (secrets redacted) ─────────────────────────
    def safe_dict(self) -> dict:
        """Return all settings with secrets redacted — safe for logging."""
        out: dict = {}
        for k in self.__dataclass_fields__:
            v = getattr(self, k)
            if k in _SECRET_FIELDS:
                out[k] = redact_secret(str(v)) if v else "(empty)"
            else:
                out[k] = v
        return out


def load_settings(env_path: str | Path | None = None) -> Settings:
    """
    Load settings from the environment (with .env file support).
    Validates and fails fast on errors.
    """
    if env_path is None:
        env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(str(env_path), override=True)

    def _env(name: str, default: str = "") -> str:
        return os.getenv(name, default)

    settings = Settings(
        mock_mode=_parse_bool(_env("MOCK_MODE", "true")),
        mcp_host=_env("MCP_HOST", "127.0.0.1"),
        mcp_port=_parse_int(_env("MCP_PORT", "8000"), "MCP_PORT"),
        mcp_path=_env("MCP_PATH", "/mcp"),
        allowed_origins=_parse_origins(
            _env("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
        ),
        mcp_auth_token=_env("MCP_AUTH_TOKEN", "change-me"),
        ifttt_webhook_key=_env("IFTTT_WEBHOOK_KEY"),
        ifttt_event_focus=_env("IFTTT_EVENT_FOCUS", "focus_mode"),
        ifttt_event_break=_env("IFTTT_EVENT_BREAK", "break_mode"),
        ifttt_event_restore=_env("IFTTT_EVENT_RESTORE", "restore_lights"),
        phone_platform=_env("PHONE_PLATFORM", "android").lower(),
        macrodroid_webhook_url_dnd_on=_env("MACRODROID_WEBHOOK_URL_DND_ON"),
        macrodroid_webhook_url_dnd_off=_env("MACRODROID_WEBHOOK_URL_DND_OFF"),
        pushcut_api_key=_env("PUSHCUT_API_KEY"),
        pushcut_notification_dnd_on=_env("PUSHCUT_NOTIFICATION_DND_ON"),
        pushcut_notification_dnd_off=_env("PUSHCUT_NOTIFICATION_DND_OFF"),
        focus_media_url=_env("FOCUS_MEDIA_URL"),
        default_focus_minutes=_parse_int(
            _env("DEFAULT_FOCUS_MINUTES", "25"), "DEFAULT_FOCUS_MINUTES"
        ),
        default_break_minutes=_parse_int(
            _env("DEFAULT_BREAK_MINUTES", "5"), "DEFAULT_BREAK_MINUTES"
        ),
        confirmation_ttl_seconds=_parse_int(
            _env("CONFIRMATION_TTL_SECONDS", "60"), "CONFIRMATION_TTL_SECONDS"
        ),
        adapter_timeout_seconds=_parse_int(
            _env("ADAPTER_TIMEOUT_SECONDS", "5"), "ADAPTER_TIMEOUT_SECONDS"
        ),
    )

    settings.validate()
    logger.info("Configuration loaded (mock_mode=%s)", settings.mock_mode)
    logger.debug("Settings: %s", settings.safe_dict())
    return settings
