"""
Session state machine, timer, HITL confirmation, and stats.

States: idle → focus → break → idle  (also: focus ↔ paused)
Only ONE active session at a time.
Timer runs server-side via asyncio, independent of any browser tab.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from src.events import EventBus
from src.scenes import SceneEngine

logger = logging.getLogger("ambient_focus_agent.session")


# ── Phases ─────────────────────────────────────────────────────────────────

class Phase(str, Enum):
    IDLE = "idle"
    FOCUS = "focus"
    BREAK = "break"
    PAUSED = "paused"


# Valid transitions (from → set of allowed to)
_TRANSITIONS: dict[Phase, set[Phase]] = {
    Phase.IDLE: {Phase.FOCUS},
    Phase.FOCUS: {Phase.BREAK, Phase.PAUSED, Phase.IDLE},
    Phase.PAUSED: {Phase.FOCUS, Phase.IDLE},
    Phase.BREAK: {Phase.IDLE},
}


# ── HITL Pending Confirmation ──────────────────────────────────────────────

@dataclass
class PendingConfirmation:
    confirmation_id: str
    minutes: int
    task: str
    options: dict[str, bool]  # {dnd, music, lights}
    created_at: float = field(default_factory=time.time)
    used: bool = False

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


# ── Session Record (for stats) ─────────────────────────────────────────────

@dataclass
class SessionRecord:
    started_at: datetime
    ended_at: datetime | None = None
    focus_minutes_requested: int = 0
    focus_seconds_actual: int = 0
    completed: bool = False  # True = timer ran to zero


# ── Focus Timer ────────────────────────────────────────────────────────────

class FocusTimer:
    """
    Async countdown timer that ticks every second.

    - ``on_tick(remaining, elapsed)`` is awaited each second.
    - ``on_expire()`` is awaited when the timer reaches zero.
    - pause() / resume() preserve remaining time.
    - cancel() stops the timer without triggering on_expire.
    """

    def __init__(
        self,
        total_seconds: int,
        on_tick: Callable[[int, int], Coroutine],
        on_expire: Callable[[], Coroutine],
    ) -> None:
        self._total = total_seconds
        self._remaining = total_seconds
        self._elapsed = 0
        self._on_tick = on_tick
        self._on_expire = on_expire
        self._task: asyncio.Task | None = None
        self._cancelled = False

    def start(self) -> None:
        self._cancelled = False
        self._task = asyncio.create_task(self._run(), name="focus-timer")

    async def _run(self) -> None:
        try:
            while self._remaining > 0:
                await asyncio.sleep(1)
                if self._cancelled:
                    return
                self._remaining -= 1
                self._elapsed += 1
                await self._on_tick(self._remaining, self._elapsed)
            if not self._cancelled:
                await self._on_expire()
        except asyncio.CancelledError:
            pass

    def pause(self) -> None:
        """Stop counting but preserve remaining time."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    def resume(self) -> None:
        """Continue counting from where we left off."""
        if self._task is None and self._remaining > 0 and not self._cancelled:
            self._task = asyncio.create_task(self._run(), name="focus-timer")

    def cancel(self) -> None:
        """Stop the timer permanently."""
        self._cancelled = True
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def elapsed(self) -> int:
        return self._elapsed

    @property
    def total(self) -> int:
        return self._total

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()


# ── Session Manager ────────────────────────────────────────────────────────

class SessionManager:
    """
    Manages the single active focus session, HITL confirmations, and stats.
    """

    def __init__(
        self,
        scene_engine: SceneEngine,
        event_bus: EventBus,
        confirmation_ttl: int = 60,
        default_focus_minutes: int = 25,
        default_break_minutes: int = 5,
    ) -> None:
        self.scene_engine = scene_engine
        self.event_bus = event_bus
        self.confirmation_ttl = confirmation_ttl
        self.default_focus_minutes = default_focus_minutes
        self.default_break_minutes = default_break_minutes

        self._phase: Phase = Phase.IDLE
        self._timer: FocusTimer | None = None
        self._task_name: str = ""
        self._session_options: dict[str, bool] = {}
        self._pre_focus_light_state: dict = {"color": "white", "brightness": 100}
        self._session_start: datetime | None = None

        # HITL confirmations
        self._pending: dict[str, PendingConfirmation] = {}

        # Stats
        self._records: list[SessionRecord] = []

        # Break timer
        self._break_timer: FocusTimer | None = None

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def remaining_seconds(self) -> int:
        if self._timer:
            return self._timer.remaining
        if self._break_timer:
            return self._break_timer.remaining
        return 0

    @property
    def elapsed_seconds(self) -> int:
        if self._timer:
            return self._timer.elapsed
        return 0

    @property
    def task_name(self) -> str:
        return self._task_name

    def get_status(self) -> dict:
        """Full session status for the status tool / dashboard."""
        scene_state: dict[str, Any] = {}
        if self._phase == Phase.FOCUS or self._phase == Phase.PAUSED:
            scene_state = {
                "lights": self.scene_engine.lights.get_last_state(),
                "dnd": "engaged",
                "music": "playing" if self._phase == Phase.FOCUS else "paused",
            }
        elif self._phase == Phase.BREAK:
            scene_state = {
                "lights": self.scene_engine.lights.get_last_state(),
                "dnd": "off",
                "music": "stopped",
            }

        return {
            "phase": self._phase.value,
            "remaining_seconds": self.remaining_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "task": self._task_name,
            "scene_state": scene_state,
            "options": self._session_options,
        }

    # ── State transition helpers ───────────────────────────────────────

    def _assert_transition(self, target: Phase) -> None:
        allowed = _TRANSITIONS.get(self._phase, set())
        if target not in allowed:
            raise InvalidTransition(
                f"Cannot transition from '{self._phase.value}' to '{target.value}'. "
                f"Allowed: {', '.join(p.value for p in allowed) or 'none'}"
            )

    def _set_phase(self, target: Phase) -> None:
        old = self._phase
        self._phase = target
        logger.info("Phase: %s → %s", old.value, target.value)

    # ── HITL: Request (creates pending confirmation) ───────────────────

    def create_focus_request(
        self,
        minutes: int | None = None,
        task: str = "",
        options: dict[str, bool] | None = None,
    ) -> dict:
        """
        Create a pending focus request. Does NOT call any adapter.
        Returns the plan + confirmation_id for the HITL confirm step.
        """
        if self._phase != Phase.IDLE:
            return {
                "error": True,
                "message": (
                    f"A session is already active (phase={self._phase.value}). "
                    "End or cancel it first."
                ),
            }

        mins = minutes or self.default_focus_minutes
        opts = {
            "dnd": True,
            "music": True,
            "lights": True,
            **(options or {}),
        }

        confirmation_id = secrets.token_urlsafe(16)
        self._pending[confirmation_id] = PendingConfirmation(
            confirmation_id=confirmation_id,
            minutes=mins,
            task=_sanitize_task(task),
            options=opts,
        )

        # Build the spoken prompt
        parts: list[str] = []
        if opts.get("lights"):
            parts.append("dim your lights to focus blue")
        if opts.get("dnd"):
            parts.append("silence your phone")
        if opts.get("music"):
            parts.append("start ambient music")
        action_str = ", ".join(parts[:-1]) + (" and " + parts[-1] if len(parts) > 1 else parts[0] if parts else "set up your environment")

        prompt = (
            f"I'll set up a {mins}-minute deep-focus session"
            + (f" for '{_sanitize_task(task)}'" if task else "")
            + f". This will {action_str}. Shall I proceed?"
        )

        return {
            "error": False,
            "confirmation_id": confirmation_id,
            "plan": {
                "minutes": mins,
                "task": _sanitize_task(task),
                "options": opts,
            },
            "spoken_prompt": prompt,
        }

    # ── HITL: Confirm ──────────────────────────────────────────────────

    async def confirm_focus(
        self,
        confirmation_id: str,
        approved: bool,
        overrides: dict[str, bool] | None = None,
    ) -> dict:
        """
        Confirm or decline a pending focus request.
        If approved, starts the session and applies the focus scene.
        """
        pending = self._pending.get(confirmation_id)

        if pending is None:
            return {"error": True, "message": "Unknown confirmation_id."}
        if pending.used:
            return {"error": True, "message": "This confirmation has already been used."}
        if pending.is_expired(self.confirmation_ttl):
            pending.used = True
            return {"error": True, "message": "Confirmation expired. Please make a new request."}

        pending.used = True

        if not approved:
            return {
                "error": False,
                "message": "Focus session declined. Nothing changed.",
                "phase": self._phase.value,
            }

        # Merge overrides
        options = {**pending.options, **(overrides or {})}

        return await self._start_focus(
            minutes=pending.minutes,
            task=pending.task,
            options=options,
        )

    # ── Start focus session ────────────────────────────────────────────

    async def _start_focus(
        self,
        minutes: int,
        task: str,
        options: dict[str, bool],
    ) -> dict:
        self._assert_transition(Phase.FOCUS)

        # Remember pre-focus light state for restore
        self._pre_focus_light_state = self.scene_engine.lights.get_last_state()
        self._task_name = task
        self._session_options = options
        self._session_start = datetime.now(timezone.utc)

        # Apply focus scene
        results = await self.scene_engine.apply_scene("focus", overrides=options)

        # Start timer
        total = minutes * 60
        self._timer = FocusTimer(
            total_seconds=total,
            on_tick=self._on_tick,
            on_expire=self._on_timer_expire,
        )
        self._timer.start()

        self._set_phase(Phase.FOCUS)
        await self.event_bus.emit("session_started", {
            "minutes": minutes,
            "task": task,
            "options": options,
        })
        await self.event_bus.emit("phase_changed", {"phase": "focus"})

        return {
            "error": False,
            "message": f"Focus mode activated for {minutes} minutes.",
            "phase": "focus",
            "remaining_seconds": total,
            "adapter_results": {k: v.to_dict() for k, v in results.items()},
        }

    # ── Timer callbacks ────────────────────────────────────────────────

    async def _on_tick(self, remaining: int, elapsed: int) -> None:
        await self.event_bus.emit("tick", {
            "remaining": remaining,
            "elapsed": elapsed,
            "phase": self._phase.value,
        })

    async def _on_timer_expire(self) -> None:
        logger.info("Focus timer expired. Transitioning to break.")

        # Record stats
        if self._timer:
            self._records.append(SessionRecord(
                started_at=self._session_start or datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                focus_minutes_requested=self._timer.total // 60,
                focus_seconds_actual=self._timer.elapsed,
                completed=True,
            ))

        self._set_phase(Phase.BREAK)
        await self.event_bus.emit("phase_changed", {"phase": "break"})

        # Apply break scene
        await self.scene_engine.apply_scene("break")

        # Start break timer
        break_total = self.default_break_minutes * 60
        self._break_timer = FocusTimer(
            total_seconds=break_total,
            on_tick=self._on_tick,
            on_expire=self._on_break_expire,
        )
        self._break_timer.start()

    async def _on_break_expire(self) -> None:
        logger.info("Break timer expired. Returning to idle.")
        await self._restore_and_idle("Break complete")

    # ── Pause / Resume ─────────────────────────────────────────────────

    async def pause(self) -> dict:
        self._assert_transition(Phase.PAUSED)
        if self._timer:
            self._timer.pause()
        self._set_phase(Phase.PAUSED)
        await self.event_bus.emit("phase_changed", {"phase": "paused"})
        return {
            "error": False,
            "message": f"Session paused. {self.remaining_seconds}s remaining.",
            "phase": "paused",
            "remaining_seconds": self.remaining_seconds,
        }

    async def resume(self) -> dict:
        self._assert_transition(Phase.FOCUS)
        if self._timer:
            self._timer.resume()
        self._set_phase(Phase.FOCUS)
        await self.event_bus.emit("phase_changed", {"phase": "focus"})
        return {
            "error": False,
            "message": f"Session resumed. {self.remaining_seconds}s remaining.",
            "phase": "focus",
            "remaining_seconds": self.remaining_seconds,
        }

    # ── End session ────────────────────────────────────────────────────

    async def end(self, reason: str = "") -> dict:
        if self._phase == Phase.IDLE:
            return {"error": True, "message": "No active session to end."}

        # Record partial stats if ending during focus
        if self._phase in (Phase.FOCUS, Phase.PAUSED) and self._timer:
            self._records.append(SessionRecord(
                started_at=self._session_start or datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                focus_minutes_requested=self._timer.total // 60,
                focus_seconds_actual=self._timer.elapsed,
                completed=False,
            ))

        return await self._restore_and_idle(reason or "Session ended by user")

    async def _restore_and_idle(self, reason: str) -> dict:
        # Cancel timers
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._break_timer:
            self._break_timer.cancel()
            self._break_timer = None

        # Restore pre-focus light state
        results = await self.scene_engine.apply_scene(
            "restore",
            restore_state=self._pre_focus_light_state,
        )

        self._set_phase(Phase.IDLE)
        self._task_name = ""
        self._session_options = {}

        await self.event_bus.emit("session_ended", {"reason": reason})
        await self.event_bus.emit("phase_changed", {"phase": "idle"})

        return {
            "error": False,
            "message": f"Session ended: {reason}. Environment restored.",
            "phase": "idle",
            "adapter_results": {k: v.to_dict() for k, v in results.items()},
        }

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self, period: str) -> dict:
        now = datetime.now(timezone.utc)
        if period == "today":
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            cutoff = now - timedelta(days=7)
        else:
            cutoff = datetime.min.replace(tzinfo=timezone.utc)

        records = [r for r in self._records if r.started_at >= cutoff]
        return {
            "period": period,
            "completed_pomodoros": sum(1 for r in records if r.completed),
            "total_focus_minutes": sum(r.focus_seconds_actual for r in records) // 60,
            "total_sessions": len(records),
        }

    # ── Cleanup ────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Cancel all timers for graceful shutdown."""
        if self._timer:
            self._timer.cancel()
        if self._break_timer:
            self._break_timer.cancel()
        logger.info("SessionManager shut down.")


# ── Helpers ────────────────────────────────────────────────────────────────

class InvalidTransition(Exception):
    """Raised when an invalid state transition is attempted."""
    pass


def _sanitize_task(task: str) -> str:
    """
    Sanitise the task string — treat as untrusted text.
    Never put into URLs, shell commands, or HTML.
    """
    if not task:
        return ""
    # Limit length, strip control chars
    cleaned = "".join(c for c in task[:200] if c.isprintable())
    return cleaned.strip()
