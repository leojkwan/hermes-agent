"""Atomic pending-turn intent in the existing profile-scoped goal record."""

from __future__ import annotations

import logging
import math
import os
import time
import uuid

logger = logging.getLogger(__name__)
RECOVERY_DELAY_SECONDS = 60


def finite_continuation_deadline(value) -> float | None:
    """Return a persisted deadline only when it can be compared safely."""
    try:
        deadline = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return deadline if math.isfinite(deadline) else None


def _claim_owner_alive(pending: dict) -> bool:
    import psutil

    pid, started_at = pending.get("claim_pid"), pending.get("claim_started_at")
    if not pid or not started_at:
        return True  # no verified stale-owner identity, so takeover is unsafe
    try:
        process = psutil.Process(int(pid))
        return process.create_time() == float(started_at) and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


class GoalContinuationMixin:
    def _refresh_control_goal(self) -> None:
        """Explicit user controls target the current record, not a cached turn snapshot."""
        from hermes_cli.goals import load_goal_record

        raw, state = load_goal_record(self.session_id)
        if raw != self._stored_raw:
            self._stored_raw, self._state = raw, state
        self._bound_goal_id = self._state.goal_id if self._state else ""

    def _reload_goal_record(self) -> bool:
        from hermes_cli.goals import load_goal_record

        self._stored_raw, self._state = load_goal_record(self.session_id)
        return bool(self._state and self._state.goal_id == self._bound_goal_id)

    def _new_continuation(self, prompt: str, kind: str, reason: str = "", due_at=None) -> dict:
        return {
            "id": uuid.uuid4().hex, "goal_id": self._state.goal_id,
            "prompt": prompt, "kind": kind, "reason": reason,
            "due_at": time.time() if due_at is None else float(due_at),
            "attempt": self._state.recovery_attempts, "claim_token": None,
        }

    def _write_goal_record(self) -> bool:
        from hermes_cli.goals import _get_session_db, _meta_key

        db = _get_session_db()
        if db is None or self._state is None:
            return False
        raw = self._state.to_json()
        key = _meta_key(self.session_id)
        try:
            if self._stored_raw is None:
                changed = db._write_rowcount(
                    "INSERT OR IGNORE INTO state_meta (key, value) VALUES (?, ?)", (key, raw))
            else:
                changed = db._write_rowcount(
                    "UPDATE state_meta SET value = ? WHERE key = ? AND value = ?",
                    (raw, key, self._stored_raw))
        except Exception:
            logger.warning("Goal continuation write failed for %s", self.session_id, exc_info=True)
            return False
        if changed:
            self._stored_raw = raw
            return True
        self._reload_goal_record()
        return False

    def _persist_goal_state(self):
        state = self._state
        if state.status != "active":
            state.pending_continuation = None
        elif state.pending_continuation is None:
            waiting = bool(state.waiting_on_pid or state.waiting_on_session or state.waiting_until)
            state.pending_continuation = self._new_continuation(
                self.next_continuation_prompt(), "wait" if waiting else "continue",
                due_at=state.waiting_until or None)
        self._last_write_succeeded = self._write_goal_record()
        return self._state

    def get_pending_continuation(self) -> dict | None:
        self._reload_goal_record()
        state = self._state
        if not state or state.status != "active":
            return None
        pending = state.pending_continuation
        if not isinstance(pending, dict) or pending.get("goal_id") != state.goal_id:
            return None
        return dict(pending)

    def pause_invalid_continuation(self, intent_id: str) -> bool:
        """Fail closed only for the same persisted intent; never pause a replaced goal."""
        if not self._reload_goal_record():
            return False
        state = self._state
        if state is None or state.status != "active":
            return False
        pending = state.pending_continuation
        if not isinstance(pending, dict) or pending.get("id") != intent_id:
            return False
        if finite_continuation_deadline(pending.get("due_at") or 0) is not None:
            return False
        getattr(self, "_pause_state")("invalid continuation deadline")
        return self._last_write_succeeded

    def prepare_continuation(
        self, prompt: str, *, kind="continue", reason="", due_at=None, max_attempts=3,
    ) -> dict | None:
        if not self._reload_goal_record() or not self.is_active() or not prompt:
            return None
        state = self._state
        pending = state.pending_continuation
        if pending and pending.get("claim_token"):
            return None
        recovering = kind in {"retryable_failure", "compression_failure"}
        if recovering and pending and pending.get("kind") == kind:
            return dict(pending)
        if recovering:
            if state.recovery_attempts >= max_attempts:
                self._pause_state(f"{kind} recovery exhausted ({state.recovery_attempts} attempts): {reason}")
                return None
            state.recovery_attempts += 1
        intent = self._new_continuation(prompt, kind, reason, due_at)
        if pending:
            intent["id"] = pending["id"]
        state.pending_continuation = intent
        return dict(intent) if self._write_goal_record() else None

    def claim_continuation(self, intent_id: str, *, recover=False, now=None) -> dict | None:
        """Caller must first hold the native writer/turn admission. ``recover`` additionally
        requires no marker-owned turn; the existing session lease, not a new lease, owns liveness."""
        if not self._reload_goal_record() or not self.is_active():
            return None
        state = self._state
        pending = state.pending_continuation
        if not pending or pending.get("id") != intent_id:
            return None
        if state.turns_used >= state.max_turns:
            self._pause_state(f"turn budget exhausted ({state.turns_used}/{state.max_turns})")
            return None
        if self.is_waiting():
            return None
        pending = self._state.pending_continuation
        if not pending or pending.get("id") != intent_id:
            return None
        due_at = finite_continuation_deadline(pending.get("due_at") or 0)
        if due_at is None:
            self._pause_state("invalid continuation deadline")
            return None
        if due_at > (time.time() if now is None else now):
            return None
        if pending.get("claim_token"):
            if not recover or _claim_owner_alive(pending):
                return None
        import psutil

        pending["claim_token"] = uuid.uuid4().hex
        pending["claim_pid"] = os.getpid()
        pending["claim_started_at"] = psutil.Process(os.getpid()).create_time()
        return dict(pending) if self._write_goal_record() else None

    def _retire_continuation(self, intent_id: str | None, claim_token: str | None, *, release=False) -> bool:
        if not self._reload_goal_record() or not self.is_active():
            return False
        pending = self._state.pending_continuation
        if not pending or (intent_id is not None and pending.get("id") != intent_id):
            return False
        if claim_token is not None and pending.get("claim_token") != claim_token:
            return False
        if release:
            pending["claim_token"] = None
            pending.pop("claim_pid", None)
            pending.pop("claim_started_at", None)
        else:
            self._state.pending_continuation = None
        return self._write_goal_record()

    def release_continuation(self, intent_id: str, claim_token: str) -> bool:
        return self._retire_continuation(intent_id, claim_token, release=True)

    def acknowledge_continuation(self, intent_id: str, claim_token: str) -> bool:
        """Only after the native durable turn marker owns crash recovery."""
        return self._retire_continuation(intent_id, claim_token)

    def discard_continuation(self, intent_id: str | None = None) -> bool:
        return self._retire_continuation(intent_id, None)

    def record_turn_failure(self, result: dict) -> dict:
        reason = str(result.get("failure_reason") or result.get("final_response") or "turn failed")[:500]
        if not self._reload_goal_record() or not self.is_active():
            return {"should_continue": False, "pending_continuation": None, "message": ""}
        interrupted = bool(result.get("interrupted") or result.get("cancelled"))
        compression = bool(result.get("compression_exhausted"))
        if interrupted or (not compression and result.get("failure_retryable") is not True):
            self._pause_state("user-interrupted" if interrupted else reason)
            if not self._last_write_succeeded:
                return {"should_continue": False, "pending_continuation": None, "message": ""}
            return {"should_continue": False, "pending_continuation": None,
                    "message": f"⏸ Goal paused: {self._state.paused_reason}"}
        now = time.time()
        try:
            resets_at = float(result.get("failure_resets_at") or 0)
        except (TypeError, ValueError):
            resets_at = 0
        due_at = (now if compression else
                  max(now + RECOVERY_DELAY_SECONDS, resets_at) if math.isfinite(resets_at) else
                  now + RECOVERY_DELAY_SECONDS)
        pending = self.prepare_continuation(
            self.next_continuation_prompt(), kind="compression_failure" if compression else "retryable_failure",
            reason=reason, due_at=due_at, max_attempts=1 if compression else 3)
        message = (f"↻ Goal recovery scheduled ({pending['attempt']}/{'1' if compression else '3'}): {reason}"
                   if pending else f"⏸ Goal paused: {self._state.paused_reason}")
        return {"should_continue": False, "pending_continuation": pending, "message": message}
