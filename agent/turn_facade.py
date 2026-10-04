"""``AIAgent.run_conversation`` / ``chat`` façade.

Turn admission around ``conversation_loop.run_conversation``: durable cross-process session turn lease +
refresher thread and liveness watchdog (``agent.turn_facade_lease``), relay/accounting/portal scopes, and
balanced start/finish marks. Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s
MRO unchanged.
"""
import logging
import time
import uuid
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from agent.lazy_forward import forward as _forward

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("run_agent")
native_turn_context: ContextVar[tuple[str, int] | None] = ContextVar("native_turn_context", default=None)


def emit_turn_progress(agent, stage: str, state: str, **fields) -> None:
    """Observer-only native facts. Copied worker context keeps its producing turn."""
    context = native_turn_context.get()
    callback = getattr(agent, "event_callback", None)
    if context is None or not callable(callback):
        return
    turn_id, started_ns = context
    payload = {"turn_id": turn_id, "stage": stage, "state": state,
               "elapsed_ms": max(0, time.monotonic_ns() - started_ns) / 1_000_000}
    payload.update({key: value for key, value in fields.items() if value is not None and key in {
        "duration_ms", "api_request_id", "retry_count", "iteration", "outcome"}})
    try:
        callback("turn:progress", payload)
    except Exception:
        logger.debug("turn progress observer failed", exc_info=True)


@contextmanager
def observe_turn_phase(agent, stage: str, **fields):
    started_ns = time.monotonic_ns()
    emit_turn_progress(agent, stage, "start", **fields)
    outcome = {"value": "returned"}
    try:
        yield outcome
    except BaseException as exc:
        outcome["value"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else "raised"
        raise
    finally:
        emit_turn_progress(agent, stage, "end", **fields, outcome=outcome["value"],
                           duration_ms=max(0, time.monotonic_ns() - started_ns) / 1_000_000)


def allocate_turn_id(session_id: str, task_id: str) -> str:
    """Reserve the same native identity used by the durable lease and relay."""
    return f"{session_id or 'session'}:{task_id}:{uuid.uuid4().hex[:8]}"


class TurnFacadeMixin:
    """run_conversation()/chat() (see module docstring)."""

    def run_conversation(
        self, user_message: Any, system_message: str=None,
        conversation_history: List[Dict[str, Any]]=None, task_id: str=None,
        stream_callback: Optional[callable]=None, persist_user_message: Optional[Any]=None,
        persist_user_timestamp: Optional[float]=None, persist_user_display_kind: Optional[str]=None,
        persist_user_display_metadata: Optional[Dict[str, Any]]=None,
        persist_user_platform_id: Optional[str]=None, moa_config: Optional[dict[str, Any]]=None,
        turn_author: Optional[Dict[str, Any]] = None,
        relay_metadata: Optional[Dict[str, Any]] = None,
        title_user_message: Optional[str]=None,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.conversation_loop.run_conversation``."""
        # A review shares this session_id for cache parity: fence review startup or interrupt
        # an admitted request and await its exit before opening live-turn instrumentation.
        # Foreground priority is retained if the review does not acknowledge within the bounded deadline
        # (#84423).
        from agent.background_review import cancel_background_review_for_live_turn

        cancel_background_review_for_live_turn(self)

        from agent import relay_runtime
        from agent.aux_accounting import reset_accounting_context, set_accounting_context
        from agent.auxiliary_client import scoped_runtime_main
        from agent.conversation_loop import run_conversation
        from agent.portal_tags import (
            reset_affinity_scope, reset_conversation_context, set_affinity_scope,
            set_conversation_context,
        )
        from agent.prompt_cache_scope import declared_conversation_scope_safe
        from agent.relay_cwd import resolve_relay_scope_cwds
        from agent.review_idle_queue import QUEUE as _review_queue
        from agent.subagent_lifecycle import bind_subagent_parent
        from agent.interrupt_scope import track_in_interrupt_scope
        from agent.turn_facade_lease import admit_durable_turn_lease, carry_unadmitted_user_message
        from hermes_cli.observability.relay_shared_metrics import finish_task_run, start_task_run

        effective_task_id = task_id or str(uuid.uuid4())
        session_id = str(getattr(self, "session_id", None) or "")
        task_context = {
            "session_id": session_id,
            "task_id": effective_task_id,
            "platform": getattr(self, "platform", None) or "",
        }
        relay_turn_id = turn_id or allocate_turn_id(session_id, effective_task_id)
        progress_token = native_turn_context.set((relay_turn_id, time.monotonic_ns()))
        self._relay_pending_turn_id = relay_turn_id
        relay_parent_session_id = (
            str(getattr(self, "_parent_session_id", None) or "")
            if task_context["platform"] == "subagent"
            else ""
        )
        relay_lease = relay_turn = lease = admission = None
        # Scope tokens start None: early returns leave the try before the set_*() calls and
        # the finally resets each one unconditionally.
        token = affinity_token = acct_token = None
        task_started = task_finished = False
        relay_outcome = "failed"

        try:
            # First statement of the try so the finally's note_turn_finished balances every exit.
            _review_queue.note_turn_started()
            with observe_turn_phase(self, "admission") as admission_outcome:
                admission = admit_durable_turn_lease(
                    self, session_id=session_id, relay_turn_id=relay_turn_id, task_context=task_context,
                    conversation_history=conversation_history,
                )
                if admission.early_result is not None:
                    admission_outcome["value"] = "refused"
            if admission.early_result is not None:
                admission.early_result["turn_id"] = relay_turn_id
                carry_unadmitted_user_message(
                    admission.early_result, user_message, persist_user_message,
                    timestamp=persist_user_timestamp, display_kind=persist_user_display_kind,
                    display_metadata=persist_user_display_metadata, platform_id=persist_user_platform_id,
                )
                relay_outcome = (
                    "cancelled" if admission.early_result.get("interrupted") else "timed_out"
                )
                return admission.early_result
            lease = admission.lease
            conversation_history = admission.conversation_history

            relay_session_cwd, relay_turn_cwd = resolve_relay_scope_cwds(
                self,
                effective_task_id,
                task_context["session_id"],
                task_context["platform"],
            )
            relay_lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
                profile_key=relay_runtime.current_profile_key(),
                session_id=task_context["session_id"], platform=task_context["platform"],
                parent_session_id=relay_parent_session_id,
                model=str(getattr(self, "model", None) or ""),
                session_cwd=relay_session_cwd,
                turn_cwd=relay_turn_cwd,
            )
            relay_turn_kwargs: Dict[str, Any] = {
                "turn_id": relay_turn_id,
                "task_id": effective_task_id,
            }
            if relay_metadata:
                relay_turn_kwargs["metadata"] = relay_metadata
            relay_turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
                relay_lease, **relay_turn_kwargs
            )
            # Minimal relay-runtime shims may lack the opt-out flag: default enabled.
            if getattr(relay_turn, "relay_enabled", True):
                start_task_run(
                    **task_context,
                    parent_session_id=getattr(self, "_parent_session_id", None) or "",
                )
                task_started = True
            # Ambient Nous Portal tagging: every LLM call in this turn (loop, compression,
            # vision, MoA, review forks) inherits `conversation=<root>`; host-declared
            # affinity scope falls back to it; accounting handles route aux usage to the session.
            token = set_conversation_context(self._conversation_root_id())
            affinity_token = set_affinity_scope(declared_conversation_scope_safe(self))
            # Publish the session accounting handles the same way so auxiliary calls record their token
            # usage into session_model_usage (task dimension) — the fix for aux spend being invisible in
            # analytics (issue #23270).
            acct_token = set_accounting_context(
                getattr(self, "_session_db", None), getattr(self, "session_id", None)
            )

            # Keep the ContextVar scope local (agent tokens may be observed from another thread).
            # A host that owns this thread (Hermes Console) may cancel the turn cross-thread.
            with bind_subagent_parent(self), scoped_runtime_main({}), track_in_interrupt_scope(self):
                try:
                    if lease is not None:
                        lease.start()
                    result = run_conversation(
                        self, user_message, system_message, conversation_history, effective_task_id,
                        stream_callback, persist_user_message,
                        persist_user_timestamp=persist_user_timestamp,
                        persist_user_display_kind=persist_user_display_kind,
                        persist_user_display_metadata=persist_user_display_metadata,
                        persist_user_platform_id=persist_user_platform_id, moa_config=moa_config,
                        turn_author=turn_author,
                        title_user_message=title_user_message,
                    )
                finally:
                    # Post-loop relay/task finalization must not receive a late refresh interrupt;
                    # the interrupt clear itself waits for the thread join in the outer finally.
                    if lease is not None:
                        lease.stop_refresher()
            terminal = result if isinstance(result, dict) else {}
            if isinstance(result, dict):
                result["turn_id"] = relay_turn_id
            relay_outcome = (
                "cancelled" if terminal.get("interrupted") is True
                else "failed" if terminal.get("failed") is True
                else "success"
            )
            relay_runtime.SESSION_COORDINATOR.finish_logical_calls(relay_turn, outcome=relay_outcome)
            if task_started:
                task_finished = True
                finish_task_run(**task_context, result=result)
            return result
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, InterruptedError)) or (
                type(exc).__name__ == "CancelledError"
            ):
                relay_outcome = "cancelled"
            elif isinstance(exc, TimeoutError):
                relay_outcome = "timed_out"
            if relay_turn is not None:
                relay_runtime.SESSION_COORDINATOR.finish_logical_calls(
                    relay_turn, outcome=relay_outcome
                )
            if task_started and not task_finished:
                task_finished = True
                finish_task_run(**task_context, error=exc)
            raise
        finally:
            from agent.interrupt_control import _ic_lock
            with _ic_lock(self, "_pending_steer_lock"):
                with _ic_lock(self, "_pending_redirect_lock"):
                    if (admission is not None and admission.early_result is None
                            and getattr(self, "_current_turn_id", None) == relay_turn_id):
                        self._native_correction_turn_active = False
            native_turn_context.reset(progress_token)
            try:
                if relay_turn is not None:
                    relay_runtime.SESSION_COORDINATOR.end_turn(relay_turn, outcome=relay_outcome)
            finally:
                try:
                    if relay_lease is not None:
                        relay_runtime.SESSION_COORDINATOR.release_conversation(relay_lease)
                finally:
                    if lease is not None:
                        lease.stop_refresher()
                        lease.join_threads()
                        lease.clear_interrupt()  # refresher interrupt between stop and join; AFTER join
                        lease.release()
                    # Always clear mid-turn labels on exit — including interrupted early returns
                    # that skip finalize_turn. Keep ts.
                    with suppress(Exception):
                        self._reset_activity_labels_after_turn()
                    if getattr(self, "_relay_pending_turn_id", None) == relay_turn_id:
                        self._relay_pending_turn_id = None
                    if acct_token is not None:
                        reset_accounting_context(acct_token)
                    if token is not None:
                        reset_conversation_context(token)
                    if affinity_token is not None:
                        reset_affinity_scope(affinity_token)
                    # Balance note_turn_started so the idle queue's live-turn count cannot leak.
                    with suppress(Exception):
                        _review_queue.note_turn_finished()

    def chat(self, message: str, stream_callback: Optional[callable] = None) -> str:
        """Final response string of one turn; ``stream_callback`` receives each text delta."""
        return self.run_conversation(message, stream_callback=stream_callback)["final_response"]

    _run_codex_app_server_turn = _forward("agent.codex_runtime", "run_codex_app_server_turn")
