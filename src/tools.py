"""
MCP tool definitions and handlers.

Each tool has:
- a JSON Schema ``inputSchema`` (the LLM reads these)
- a clear ``description``
- a handler that validates inputs, calls the session manager,
  and returns structured JSON results
"""

from __future__ import annotations

import logging
import contextvars
import uuid
from typing import Any

from src.session import SessionManager, InvalidTransition

logger = logging.getLogger("ambient_focus_agent.tools")

# ── Correlation ID for structured logging ──────────────────────────────────
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def _new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:10]
    correlation_id.set(cid)
    return cid


# ── Tool schema definitions ───────────────────────────────────────────────

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "ambient_focus_request",
        "description": (
            "Request a deep-focus session. Does NOT change the environment yet — "
            "creates a pending request with a confirmation_id that must be approved "
            "via ambient_focus_confirm before anything happens. Returns a spoken "
            "prompt the assistant can read to the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 120,
                    "description": "Focus duration in minutes (5–120). Defaults to 25.",
                },
                "task": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Optional short description of what the user is working on.",
                },
                "options": {
                    "type": "object",
                    "description": "Toggle individual adapters on/off for this session.",
                    "properties": {
                        "dnd": {"type": "boolean", "description": "Enable phone Do Not Disturb."},
                        "music": {"type": "boolean", "description": "Play ambient focus music."},
                        "lights": {"type": "boolean", "description": "Switch lights to focus blue."},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ambient_focus_confirm",
        "description": (
            "Confirm or decline a pending focus request (human-in-the-loop). "
            "approved=true starts the session and applies the focus scene. "
            "approved=false cancels with no side effects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirmation_id": {
                    "type": "string",
                    "description": "The confirmation_id returned by ambient_focus_request.",
                },
                "approved": {
                    "type": "boolean",
                    "description": "True to start, false to cancel.",
                },
                "overrides": {
                    "type": "object",
                    "description": "Override adapter toggles from the original request.",
                    "properties": {
                        "dnd": {"type": "boolean"},
                        "music": {"type": "boolean"},
                        "lights": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["confirmation_id", "approved"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ambient_focus_status",
        "description": (
            "Get the current session status: phase (idle|focus|break|paused), "
            "remaining seconds, task, and scene state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "ambient_focus_pause",
        "description": "Pause the active focus timer. Remaining time is preserved.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "ambient_focus_resume",
        "description": "Resume a paused focus timer.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "ambient_focus_end",
        "description": (
            "End the current focus or break session. Restores the previous "
            "environment (lights, DND, music)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Optional reason for ending the session.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "set_lights",
        "description": (
            "Directly control the smart lights. Still respects MOCK_MODE. "
            "Can be used independently of a focus session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "color": {
                    "type": "string",
                    "enum": ["blue", "green", "warm_white", "white"],
                    "description": "Light colour preset.",
                },
                "brightness": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Brightness percentage (0–100).",
                },
            },
            "required": ["color", "brightness"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_session_stats",
        "description": "Get completed pomodoros and total focus minutes for a period.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "week"],
                    "description": "Time period to query.",
                },
            },
            "required": ["period"],
            "additionalProperties": False,
        },
    },
]


# ── Input validation helpers ───────────────────────────────────────────────

class ToolInputError(Exception):
    """Raised when tool input validation fails."""
    pass


def _validate_minutes(args: dict) -> int | None:
    minutes = args.get("minutes")
    if minutes is None:
        return None
    if not isinstance(minutes, int):
        raise ToolInputError("'minutes' must be an integer.")
    if minutes < 5 or minutes > 120:
        raise ToolInputError("'minutes' must be between 5 and 120.")
    return minutes


def _validate_task(args: dict) -> str:
    task = args.get("task", "")
    if not isinstance(task, str):
        raise ToolInputError("'task' must be a string.")
    if len(task) > 200:
        raise ToolInputError("'task' must be 200 characters or fewer.")
    return task


def _validate_options(args: dict, key: str = "options") -> dict[str, bool] | None:
    opts = args.get(key)
    if opts is None:
        return None
    if not isinstance(opts, dict):
        raise ToolInputError(f"'{key}' must be an object.")
    for k, v in opts.items():
        if k not in ("dnd", "music", "lights"):
            raise ToolInputError(f"Unknown option '{k}'. Allowed: dnd, music, lights.")
        if not isinstance(v, bool):
            raise ToolInputError(f"Option '{k}' must be a boolean.")
    return opts


def _validate_color(args: dict) -> str:
    color = args.get("color")
    if color not in ("blue", "green", "warm_white", "white"):
        raise ToolInputError("'color' must be one of: blue, green, warm_white, white.")
    return color


def _validate_brightness(args: dict) -> int:
    b = args.get("brightness")
    if not isinstance(b, int):
        raise ToolInputError("'brightness' must be an integer.")
    if b < 0 or b > 100:
        raise ToolInputError("'brightness' must be between 0 and 100.")
    return b


def _validate_period(args: dict) -> str:
    period = args.get("period")
    if period not in ("today", "week"):
        raise ToolInputError("'period' must be 'today' or 'week'.")
    return period


# ── Tool router ────────────────────────────────────────────────────────────

async def handle_tool_call(
    name: str,
    arguments: dict,
    session_mgr: SessionManager,
) -> dict:
    """
    Route a tool call to its handler.
    Returns a dict suitable for MCP ``tools/call`` result content.
    """
    cid = _new_correlation_id()
    logger.info("[%s] Tool call: %s", cid, name)

    try:
        if name == "ambient_focus_request":
            return _handle_focus_request(arguments, session_mgr)

        elif name == "ambient_focus_confirm":
            return await _handle_focus_confirm(arguments, session_mgr)

        elif name == "ambient_focus_status":
            return session_mgr.get_status()

        elif name == "ambient_focus_pause":
            return await session_mgr.pause()

        elif name == "ambient_focus_resume":
            return await session_mgr.resume()

        elif name == "ambient_focus_end":
            reason = arguments.get("reason", "")
            if reason and not isinstance(reason, str):
                raise ToolInputError("'reason' must be a string.")
            if reason and len(reason) > 200:
                raise ToolInputError("'reason' must be 200 characters or fewer.")
            return await session_mgr.end(reason=reason)

        elif name == "set_lights":
            return await _handle_set_lights(arguments, session_mgr)

        elif name == "get_session_stats":
            return _handle_stats(arguments, session_mgr)

        else:
            return {"error": True, "message": f"Unknown tool: {name}"}

    except ToolInputError as e:
        logger.warning("[%s] Validation error: %s", cid, e)
        return {"error": True, "message": str(e)}

    except InvalidTransition as e:
        logger.warning("[%s] Invalid transition: %s", cid, e)
        return {"error": True, "message": str(e)}

    except Exception as e:
        logger.exception("[%s] Unexpected error in tool %s", cid, name)
        return {"error": True, "message": f"Internal error: {type(e).__name__}"}


# ── Individual handlers ───────────────────────────────────────────────────

def _handle_focus_request(args: dict, mgr: SessionManager) -> dict:
    minutes = _validate_minutes(args)
    task = _validate_task(args)
    options = _validate_options(args)
    return mgr.create_focus_request(minutes=minutes, task=task, options=options)


async def _handle_focus_confirm(args: dict, mgr: SessionManager) -> dict:
    cid = args.get("confirmation_id")
    if not cid or not isinstance(cid, str):
        raise ToolInputError("'confirmation_id' is required and must be a string.")

    approved = args.get("approved")
    if not isinstance(approved, bool):
        raise ToolInputError("'approved' must be a boolean.")

    overrides = _validate_options(args, key="overrides")
    return await mgr.confirm_focus(
        confirmation_id=cid, approved=approved, overrides=overrides,
    )


async def _handle_set_lights(args: dict, mgr: SessionManager) -> dict:
    color = _validate_color(args)
    brightness = _validate_brightness(args)
    result = await mgr.scene_engine.lights.set_lights(color, brightness)
    return result.to_dict()


def _handle_stats(args: dict, mgr: SessionManager) -> dict:
    period = _validate_period(args)
    return mgr.get_stats(period)
