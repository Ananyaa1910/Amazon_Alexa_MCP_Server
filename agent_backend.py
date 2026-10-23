"""
Alexa+ Ambient Focus & Environment Sync Agent — Backend Server
===============================================================
A Human-in-the-Loop (HITL) MCP-style backend that orchestrates
smart-home webhooks (IFTTT Maker, MacroDroid/Pushcut) and exposes
a REST API consumed by the companion dashboard (index.html).

Tech: FastAPI · asyncio · httpx · threading
Licence: MIT — Amazon Developer Hackathon 2026
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
)
logger = logging.getLogger("ambient_focus_agent")

# ---------------------------------------------------------------------------
# Webhook configuration — all free-tier endpoints
# ---------------------------------------------------------------------------
# IFTTT Maker Webhooks  (lights)
IFTTT_KEY = os.getenv("IFTTT_KEY", "demo_key")
IFTTT_FOCUS_URL = f"https://maker.ifttt.com/trigger/focus_lights_on/with/key/{IFTTT_KEY}"
IFTTT_BREAK_URL = f"https://maker.ifttt.com/trigger/break_lights_on/with/key/{IFTTT_KEY}"
IFTTT_NORMAL_URL = f"https://maker.ifttt.com/trigger/normal_lights_on/with/key/{IFTTT_KEY}"

# MacroDroid / Pushcut  (phone DND)
MACRODROID_DND_ON = os.getenv(
    "MACRODROID_DND_ON",
    "https://trigger.macrodroid.com/demo/dnd_on",
)
MACRODROID_DND_OFF = os.getenv(
    "MACRODROID_DND_OFF",
    "https://trigger.macrodroid.com/demo/dnd_off",
)


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------
class FocusPhase(str, Enum):
    """Strict phases the agent can be in per user session."""

    IDLE = "idle"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    FOCUS_ACTIVE = "focus_active"
    BREAK_MODE = "break_mode"
    CANCELLED = "cancelled"


@dataclass
class UserSession:
    """Per-user focus session state."""

    user_id: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    phase: FocusPhase = FocusPhase.IDLE
    focus_duration_minutes: int = 25
    elapsed_seconds: int = 0
    remaining_seconds: int = 0
    confirmed: bool = False
    intent_text: str = ""
    lighting_status: str = "normal"          # normal | focus | break
    dnd_status: str = "off"                  # off | engaged
    music_status: str = "stopped"            # stopped | playing
    webhook_results: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error_log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "phase": self.phase.value,
            "focus_duration_minutes": self.focus_duration_minutes,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "confirmed": self.confirmed,
            "intent_text": self.intent_text,
            "lighting_status": self.lighting_status,
            "dnd_status": self.dnd_status,
            "music_status": self.music_status,
            "webhook_results": self.webhook_results,
            "created_at": self.created_at,
            "error_log": self.error_log,
        }


# ---------------------------------------------------------------------------
# In-memory session store  (multi-user safe via dict keying)
# ---------------------------------------------------------------------------
sessions: Dict[str, UserSession] = {}
countdown_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Alexa+ Ambient Focus Agent",
    version="1.0.0",
    description="Human-in-the-Loop MCP server for ambient focus orchestration.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------
class IntentRequest(BaseModel):
    user_id: str
    text: str
    confirmed: bool = False
    duration_minutes: int = 25


class ConfirmRequest(BaseModel):
    user_id: str
    session_id: str
    confirmed: bool


class CancelRequest(BaseModel):
    user_id: str
    session_id: str


# ---------------------------------------------------------------------------
# Async webhook dispatcher  (non-blocking, concurrent)
# ---------------------------------------------------------------------------
async def _fire_webhook(label: str, url: str, payload: dict | None = None) -> dict:
    """Fire a single webhook with graceful error handling."""
    result: dict = {"label": label, "url": url, "success": False, "detail": ""}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if payload:
                resp = await client.post(url, json=payload)
            else:
                resp = await client.get(url)
            result["status_code"] = resp.status_code
            result["success"] = resp.status_code < 400
            result["detail"] = resp.text[:200]
    except httpx.ConnectError:
        result["detail"] = "Connection refused — target unreachable."
        logger.warning("Webhook %s connect error: %s", label, url)
    except httpx.TimeoutException:
        result["detail"] = "Request timed out after 10 s."
        logger.warning("Webhook %s timed out: %s", label, url)
    except httpx.HTTPError as exc:
        result["detail"] = f"HTTP error: {exc}"
        logger.warning("Webhook %s HTTP error: %s", label, exc)
    except Exception as exc:
        result["detail"] = f"Unexpected error: {exc}"
        logger.exception("Webhook %s unexpected error", label)
    return result


async def dispatch_focus_webhooks() -> Dict[str, dict]:
    """Fire IFTTT focus-lights + MacroDroid DND concurrently."""
    tasks = [
        _fire_webhook("ifttt_focus_lights", IFTTT_FOCUS_URL, {"value1": "focus"}),
        _fire_webhook("macrodroid_dnd_on", MACRODROID_DND_ON),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return {r["label"]: r for r in results}


async def dispatch_break_webhooks() -> Dict[str, dict]:
    """Fire IFTTT break-lights + MacroDroid DND-off concurrently."""
    tasks = [
        _fire_webhook("ifttt_break_lights", IFTTT_BREAK_URL, {"value1": "break"}),
        _fire_webhook("macrodroid_dnd_off", MACRODROID_DND_OFF),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return {r["label"]: r for r in results}


async def dispatch_normal_webhooks() -> Dict[str, dict]:
    """Restore normal lighting via IFTTT."""
    results_list = await asyncio.gather(
        _fire_webhook("ifttt_normal_lights", IFTTT_NORMAL_URL, {"value1": "normal"}),
    )
    return {r["label"]: r for r in results_list}


# ---------------------------------------------------------------------------
# Countdown thread (runs in background, fires break callback on expiry)
# ---------------------------------------------------------------------------
def _countdown_worker(user_id: str, total_seconds: int) -> None:
    """
    Tick every second, updating session.remaining_seconds.
    On expiry, trigger break_mode webhooks via a new event loop.
    """
    stop_event = _stop_events.get(user_id)
    session = sessions.get(user_id)
    if not session or not stop_event:
        return

    session.remaining_seconds = total_seconds
    session.elapsed_seconds = 0

    for tick in range(total_seconds):
        if stop_event.is_set():
            logger.info("Countdown for %s cancelled at tick %d.", user_id, tick)
            return
        time.sleep(1)
        session.elapsed_seconds = tick + 1
        session.remaining_seconds = total_seconds - (tick + 1)

    # Timer expired — transition to break mode
    logger.info("Focus timer expired for user %s. Entering break mode.", user_id)
    session.phase = FocusPhase.BREAK_MODE
    session.lighting_status = "break"
    session.dnd_status = "off"
    session.music_status = "stopped"

    # Fire break webhooks in a fresh event loop (thread-safe)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        webhook_results = loop.run_until_complete(dispatch_break_webhooks())
        session.webhook_results.update(webhook_results)
        loop.close()
    except Exception as exc:
        error_msg = f"Break webhook dispatch failed: {exc}"
        session.error_log.append(error_msg)
        logger.exception(error_msg)


def _start_countdown(user_id: str, duration_minutes: int) -> None:
    """Spawn (or replace) the countdown thread for a user."""
    # Cancel any existing countdown
    _cancel_countdown(user_id)

    stop_event = threading.Event()
    _stop_events[user_id] = stop_event

    total_seconds = duration_minutes * 60
    thread = threading.Thread(
        target=_countdown_worker,
        args=(user_id, total_seconds),
        daemon=True,
        name=f"countdown-{user_id}",
    )
    countdown_threads[user_id] = thread
    thread.start()
    logger.info(
        "Countdown started for %s: %d min (%d s).",
        user_id,
        duration_minutes,
        total_seconds,
    )


def _cancel_countdown(user_id: str) -> None:
    """Gracefully stop a running countdown thread."""
    stop_event = _stop_events.pop(user_id, None)
    if stop_event:
        stop_event.set()
    thread = countdown_threads.pop(user_id, None)
    if thread and thread.is_alive():
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Tool router — HITL entry point
# ---------------------------------------------------------------------------
async def process_alexa_request(intent_data: IntentRequest) -> dict:
    """
    Core tool-router implementing the HITL state machine.

    Flow:
      IDLE  ─[intent received, confirmed=False]─▸  AWAITING_CONFIRMATION
      AWAITING_CONFIRMATION ─[confirmed=True]─▸  FOCUS_ACTIVE  (webhooks fire)
      FOCUS_ACTIVE ─[timer expires]─▸  BREAK_MODE  (break webhooks fire)
      Any ─[cancel]─▸  CANCELLED / IDLE
    """
    user_id = intent_data.user_id
    text = intent_data.text.lower().strip()
    confirmed = intent_data.confirmed
    duration = intent_data.duration_minutes

    # ── Retrieve or create session ──────────────────────────────────────
    session = sessions.get(user_id)
    if session is None or session.phase in (FocusPhase.CANCELLED, FocusPhase.BREAK_MODE, FocusPhase.IDLE):
        session = UserSession(user_id=user_id, intent_text=text, focus_duration_minutes=duration)
        sessions[user_id] = session

    # ── Parse intent ────────────────────────────────────────────────────
    focus_keywords = ["focus", "deep work", "concentrate", "pomodoro", "study", "work mode"]
    is_focus_request = any(kw in text for kw in focus_keywords)

    if not is_focus_request:
        return {
            "status": "unrecognised_intent",
            "message": "I didn't detect a focus-related request. Try saying 'start deep work for 25 minutes'.",
            "session": session.to_dict(),
        }

    # ── HITL gate: require explicit confirmation ────────────────────────
    if not confirmed:
        session.phase = FocusPhase.AWAITING_CONFIRMATION
        session.intent_text = text
        session.focus_duration_minutes = duration
        return {
            "status": "awaiting_confirmation",
            "message": (
                f"I'll set up a {duration}-minute deep-focus session. "
                "This will dim your lights to focus blue, enable Do Not Disturb, "
                "and play ambient music. Shall I proceed?"
            ),
            "requires_confirmation": True,
            "session": session.to_dict(),
        }

    # ── Confirmation received — activate focus mode ─────────────────────
    session.confirmed = True
    session.phase = FocusPhase.FOCUS_ACTIVE
    session.lighting_status = "focus"
    session.dnd_status = "engaged"
    session.music_status = "playing"

    # Fire webhooks asynchronously (non-blocking, concurrent)
    webhook_results = await dispatch_focus_webhooks()
    session.webhook_results = webhook_results

    # Start countdown thread
    _start_countdown(user_id, duration)

    return {
        "status": "focus_active",
        "message": (
            f"Focus mode activated for {duration} minutes. "
            "Lights dimmed to focus blue. DND engaged. Ambient music playing. "
            "I'll notify you when it's time for a break."
        ),
        "session": session.to_dict(),
    }


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "service": "Alexa+ Ambient Focus Agent",
        "version": "1.0.0",
        "status": "online",
        "endpoints": [
            "/api/intent",
            "/api/confirm",
            "/api/cancel",
            "/api/session/{user_id}",
            "/api/sessions",
        ],
    }


@app.post("/api/intent")
async def handle_intent(req: IntentRequest):
    """
    Receive a natural-language intent from the Alexa skill / dashboard.
    Implements HITL: returns a confirmation prompt unless confirmed=True.
    """
    result = await process_alexa_request(req)
    return JSONResponse(content=result)


@app.post("/api/confirm")
async def handle_confirm(req: ConfirmRequest):
    """
    Explicit user confirmation endpoint (HITL second step).
    """
    session = sessions.get(req.user_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active session for this user.")
    if session.session_id != req.session_id:
        raise HTTPException(status_code=409, detail="Session ID mismatch.")
    if session.phase != FocusPhase.AWAITING_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail=f"Session is in '{session.phase.value}', not awaiting confirmation.",
        )

    if not req.confirmed:
        # User declined
        session.phase = FocusPhase.CANCELLED
        session.confirmed = False
        _cancel_countdown(req.user_id)
        return {"status": "cancelled", "message": "Focus session cancelled.", "session": session.to_dict()}

    # Activate focus
    session.confirmed = True
    session.phase = FocusPhase.FOCUS_ACTIVE
    session.lighting_status = "focus"
    session.dnd_status = "engaged"
    session.music_status = "playing"

    webhook_results = await dispatch_focus_webhooks()
    session.webhook_results = webhook_results

    _start_countdown(req.user_id, session.focus_duration_minutes)

    return {
        "status": "focus_active",
        "message": (
            f"Focus mode activated for {session.focus_duration_minutes} minutes. "
            "All systems engaged."
        ),
        "session": session.to_dict(),
    }


@app.post("/api/cancel")
async def handle_cancel(req: CancelRequest):
    """Cancel an active focus session and restore normal state."""
    session = sessions.get(req.user_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active session for this user.")
    if session.session_id != req.session_id:
        raise HTTPException(status_code=409, detail="Session ID mismatch.")

    _cancel_countdown(req.user_id)

    session.phase = FocusPhase.CANCELLED
    session.lighting_status = "normal"
    session.dnd_status = "off"
    session.music_status = "stopped"

    # Restore normal lighting
    try:
        webhook_results = await dispatch_normal_webhooks()
        session.webhook_results.update(webhook_results)
    except Exception as exc:
        session.error_log.append(f"Normal-restore webhook failed: {exc}")

    return {
        "status": "cancelled",
        "message": "Focus session cancelled. Lights restored. DND off.",
        "session": session.to_dict(),
    }


@app.get("/api/session/{user_id}")
async def get_session(user_id: str):
    """Poll current session state (used by dashboard for live updates)."""
    session = sessions.get(user_id)
    if not session:
        # Return a default idle session so the dashboard always has data
        return {
            "status": "no_session",
            "session": UserSession(user_id=user_id).to_dict(),
        }
    return {"status": "ok", "session": session.to_dict()}


@app.get("/api/sessions")
async def list_sessions():
    """List all active sessions (admin/debug)."""
    return {
        "count": len(sessions),
        "sessions": {uid: s.to_dict() for uid, s in sessions.items()},
    }


@app.post("/api/reset/{user_id}")
async def reset_session(user_id: str):
    """Hard-reset a user session back to idle."""
    _cancel_countdown(user_id)
    sessions.pop(user_id, None)

    # Restore normal webhooks
    try:
        await dispatch_normal_webhooks()
    except Exception:
        pass

    try:
        results = await asyncio.gather(
            _fire_webhook("macrodroid_dnd_off", MACRODROID_DND_OFF),
        )
    except Exception:
        pass

    return {"status": "reset", "message": f"Session for {user_id} has been reset."}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "agent_backend:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
