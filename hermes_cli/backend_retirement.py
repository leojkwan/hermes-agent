"""Process-owned admission fence for cooperative Desktop backend retirement.

The gate belongs to the process, not a profile or transport. Diagnostic ledgers alone cannot
close the check/use race: every source of new work must reserve admission before dispatch.
"""

from contextlib import contextmanager
import logging
import secrets
import threading
import time
from contextvars import copy_context


class RetirementFence:
    """Exclusive 30-second prepare permit; commit closes admission permanently.

    Tokens are random and recognized only by this process instance. A committed prepare
    returns the same token so a client can recover a lost commit response without reopening.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._preparing = False
        self._token = None
        self._committed = False
        self._expires_at = 0.0
        self._now = time.monotonic

    def _expire(self):
        if self._token is not None and not self._committed and self._now() >= self._expires_at:
            self._token = None

    def acquire(self):
        with self._lock:
            self._expire()
            if self._preparing or self._token is not None:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            self._active -= 1

    def active_count(self):
        with self._lock:
            return self._active

    def submit_worker(self, loop, fn, *, executor=None, on_admit=None, on_release=None):
        """Hold admission until the actual thread exits, including after await cancellation.

        A canceled queued body never starts after its reservation is released. Context belongs
        to the submitting activity, exactly as with asyncio.to_thread.
        """
        context = copy_context()
        if not self.acquire():
            raise RuntimeError("Gateway admission is closed")
        state_lock = threading.Lock()
        state = {"started": False, "released": False, "counted": False}

        def release(*, only_before_start=False):
            with state_lock:
                if state["released"] or (only_before_start and state["started"]):
                    return
                state["released"] = True
            try:
                if state["counted"] and on_release is not None:
                    on_release()
            finally:
                self.release()

        def run():
            with state_lock:
                if state["released"]:
                    return None
                state["started"] = True
            try:
                return context.run(fn)
            finally:
                release()

        try:
            if on_admit is not None:
                on_admit()
                state["counted"] = True
            future = loop.run_in_executor(executor, run)
            future.add_done_callback(lambda done: release(only_before_start=True) if done.cancelled() else None)
            return future
        except BaseException:
            release(only_before_start=True)
            raise

    @contextmanager
    def work(self):
        admitted = self.acquire()
        try:
            yield admitted
        finally:
            if admitted:
                self.release()

    def prepare(self, *, idle_predicate=None):
        with self._lock:
            self._expire()
            if self._committed:
                return {"ok": True, "idle": True, "token": self._token}
            if self._preparing or self._token is not None or self._active:
                return {"ok": False, "idle": False}
            self._preparing = True
        # No admissions can cross the freeze. Do not hold our lock while reading ledgers:
        # some callers already hold a session/cron lock when reserving their work.
        verdict = None
        try:
            if idle_predicate is None:
                from hermes_cli.web_server_idle_proof import idle_proof
                verdict = idle_proof()["idle"]
            else:
                verdict = idle_predicate()
        except Exception:
            logging.getLogger(__name__).warning("Retirement idle probe unavailable", exc_info=True)
        finally:
            with self._lock:
                self._preparing = False
                if verdict is True:
                    self._token = token = secrets.token_urlsafe(32)
                    self._expires_at = self._now() + 30.0
        if verdict is not True:
            return {"ok": False, "idle": verdict}
        return {"ok": True, "idle": True, "token": token}

    def commit(self, token):
        with self._lock:
            self._expire()
            if not token or token != self._token:
                return {"ok": False}
            self._committed = True
            return {"ok": True}

    def cancel(self, token):
        with self._lock:
            self._expire()
            if not token or token != self._token or self._committed:
                return {"ok": False}
            self._token = None
            return {"ok": True}


retirement = RetirementFence()
